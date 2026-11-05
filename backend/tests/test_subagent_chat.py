"""S3 集成：turn runtime 装配 / 事件合并 / chat SSE 的 subagent block / 停止级联。"""
from __future__ import annotations

import asyncio

import pytest

from app.subagent.context import AgentContext
from app.subagent.events import EventBuffer


def run(coro):
    return asyncio.run(coro)


def _collect(gen):
    async def _r():
        out = []
        async for x in gen:
            out.append(x)
        return out
    return run(_r())


# ---------- build_turn_runtime ----------
def test_turn_runtime_injects_orchestration_tools(tmp_path, monkeypatch):
    from app import workspace
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    from app.subagent import store as sub_store
    monkeypatch.setattr(sub_store, "DB_PATH", tmp_path / "s.db")
    run(sub_store.init_tables())

    from app.subagent.session import build_turn_runtime
    from app.tools import Tool, ToolRegistry

    def mk(n):
        async def call(args):
            return "ok"
        return Tool(n, "", {"type": "object", "properties": {}}, call)

    class FakeModel:
        model_name = "fake"
        client = object()

    class FakeBaseLoop:
        system_prompt = "SYS"

    class FakeBase:
        model = FakeModel()
        loop = FakeBaseLoop()
        store = object()
        registry = ToolRegistry([mk("file_read"), mk("spawn_agent"), mk("mcp_thing")])

    ctx = AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")
    turn = build_turn_runtime(FakeBase(), ctx)
    names = set(turn.loop.registry.names())
    for t in ("spawn_agent", "wait_agent", "read_agent_result", "list_agents", "close_agent", "file_read"):
        assert t in names, t
    assert "file_patch" in names   # builtin rebuilt
    assert "mcp_thing" in names    # non-builtin MCP kept
    assert turn.loop.system_prompt == "SYS"
    # 主对话必须落 session_history（否则下一轮丢上下文）
    assert turn.loop.history_store is FakeBase.store


# ---------- merged events ----------
def test_merged_events_interleaves_and_closes():
    from app.routers.chat import _merged_events

    async def main_gen():
        yield {"type": "text_delta", "text": "A"}
        await asyncio.sleep(0.01)
        yield {"type": "text_delta", "text": "C"}

    async def scenario():
        buf = EventBuffer()
        buf.put({"type": "subagent_start", "agent_id": "sub:x", "name": "x", "role": "general", "task": "t"})
        buf.put({"type": "subagent_end", "agent_id": "sub:x", "name": "x", "status": "completed", "result": "r"})
        out = []
        async for ev in _merged_events(main_gen(), buf, object()):
            out.append(ev["type"])
        return out

    types = run(scenario())
    assert types[0] == "text_delta"
    assert "subagent_start" in types
    assert "subagent_end" in types
    # main must finish last-completed text after subagent events were queued first
    assert types.count("text_delta") == 2


# ---------- chat SSE builds subagent block ----------
def test_event_stream_builds_subagent_block(monkeypatch):
    from app.routers import chat
    from app import db

    captured = {}

    async def fake_add_message(*a, **k):
        return {"id": k.get("message_id", "m")}

    async def fake_update_message(message_id, **fields):
        captured["blocks"] = fields.get("blocks")
        captured["status"] = fields.get("status")

    class FakeLoop:
        async def run_stream(self, history, stop_event=None, session_id=""):
            yield {"type": "text_delta", "text": "开始"}
            yield {"type": "done", "status": "complete"}

    class FakeTurn:
        def __init__(self):
            self.buffer = EventBuffer()
            self.buffer.put({"type": "subagent_start", "agent_id": "sub:scan", "name": "scan",
                             "role": "explore", "task": "看看代码"})
            self.buffer.put({"type": "subagent_event", "agent_id": "sub:scan",
                             "event": {"type": "tool_start", "id": "t1", "name": "file_read",
                                       "input": {"path": "a.py"}}})
            self.buffer.put({"type": "subagent_end", "agent_id": "sub:scan", "name": "scan",
                             "status": "completed", "result": "结论", "turns": 2})
            self.buffer.close()
            self.loop = FakeLoop()
            self.store = None
            self.model = type("M", (), {"model_name": "fake"})()

        class host:
            async def close_all(self):
                pass

    class FakeHub:
        def __init__(self):
            self.registered = []

        async def get_user_runtime(self, uid, mid=""):
            class UR:
                class model:
                    model_name = "fake"

                async def sanitize_pending_tool_calls(self, sid):
                    return False
            return UR()

        def register_subagent_host(self, sid, gid, host):
            self.registered.append((sid, gid))

        def unregister_subagent_host(self, sid, gid):
            pass

    hub = FakeHub()
    monkeypatch.setattr(chat, "get_hub", lambda: hub)
    monkeypatch.setattr(chat, "build_turn_runtime", lambda ur, ctx: FakeTurn())
    monkeypatch.setattr(db, "add_message", fake_add_message)
    monkeypatch.setattr(db, "update_message", fake_update_message)
    import app.compaction as compaction
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(compaction, "enforce_soft_limit", _noop, raising=False)
    monkeypatch.setattr(compaction, "enforce_hard_limit", _noop, raising=False)

    frames = _collect(chat._event_stream("u1", "s1", "hi", "g1", "m1"))
    joined = "\n".join(frames)
    assert "event: subagent_start" in joined
    assert "event: subagent_end" in joined

    subs = [b for b in captured["blocks"] if b.get("type") == "subagent"]
    assert len(subs) == 1
    assert subs[0]["agent_id"] == "sub:scan"
    assert subs[0]["status"] == "completed"
    assert subs[0]["result"] == "结论"
    assert any(e["kind"] == "tool_start" for e in subs[0]["events"])


# ---------- stop cascade ----------
def test_stop_endpoint_cascades_to_subagents(monkeypatch):
    from app.routers import chat
    from app import db

    async def fake_get_session(sid, uid):
        return {"id": sid, "user_id": uid}

    def fake_stop(sid, gid):
        return 1

    class FakeHub:
        def __init__(self):
            self.calls = []

        async def stop_subagent_generation(self, sid, gid):
            self.calls.append((sid, gid))
            return 2

    hub = FakeHub()
    monkeypatch.setattr(chat, "get_hub", lambda: hub)
    monkeypatch.setattr(chat, "request_stop", fake_stop)
    monkeypatch.setattr(db, "get_session", fake_get_session)

    from app.schemas import StopIn
    out = run(chat.stop_generation("s1", StopIn(generation_id="g1"), {"id": "u1"}))
    assert out["ok"] is True
    assert out["subagents_stopped"] == 2
    assert hub.calls == [("s1", "g1")]
