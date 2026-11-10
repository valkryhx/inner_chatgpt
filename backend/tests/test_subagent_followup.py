"""followup_task：向运行中的子代理追加指令（steer）。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.llm_client import ChatResult
from app.tools import Tool, ToolRegistry
from app.subagent import store as sub_store
from app.subagent.context import AgentContext
from app.subagent.events import EventBuffer
from app.subagent.host import SubagentHost


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sub_store, "DB_PATH", tmp_path / "sub.db")
    run(sub_store.init_tables())
    from app import workspace
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    return tmp_path


class GatedClient:
    """第一次调用等 release，之后正常返回；记录每次 run 的 messages。"""

    def __init__(self, tool_name="file_read"):
        self.tool_name = tool_name
        self.calls = 0
        self.seen: list[list[dict]] = []
        self.ready = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, messages, tools=None, on_delta=None, stop_check=None):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self.calls == 1:
            self.ready.set()
            await self.release.wait()
            return ChatResult(content="", tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": self.tool_name, "arguments": json.dumps({"path": "x.txt"})},
            }])
        return ChatResult(content="final done")


class Model:
    def __init__(self, client):
        self.client = client
        self.model_name = "fake"


def _registry():
    async def noop(args):
        return "ok"

    def mk(n):
        return Tool(n, "", {"type": "object", "properties": {}}, noop)
    names = ["file_read", "file_write", "code_run", "load_skill", "spawn_agent",
             "wait_agent", "read_agent_result", "list_agents", "close_agent"]
    return ToolRegistry([mk(n) for n in names])


class Parent:
    def __init__(self, client):
        self.model = Model(client)
        self.registry = _registry()


def _ctx():
    return AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")


def _host(client):
    return SubagentHost(_ctx(), parent_runtime=Parent(client), buffer=EventBuffer())


def test_followup_injected_before_next_llm_call(isolated):
    async def flow():
        client = GatedClient()
        host = _host(client)
        await host.spawn(task_name="a", message="m")
        await asyncio.wait_for(client.ready.wait(), timeout=5)
        res = await host.followup("a", "请补充 X 部分")
        assert res["ok"] is True
        client.release.set()
        await host.wait(targets=["a"], timeout_s=10)
        assert len(client.seen) >= 2
        joined = " ".join(str(m.get("content") or "") for m in client.seen[1])
        assert "请补充 X 部分" in joined
        assert "父代理" in joined

    run(flow())


def test_followup_rejected_on_terminal(isolated):
    async def flow():
        client = GatedClient()
        host = _host(client)
        await host.spawn(task_name="a", message="m")
        await asyncio.wait_for(client.ready.wait(), timeout=5)
        client.release.set()
        await host.wait(targets=["a"], timeout_s=10)
        res = await host.followup("a", "太晚了")
        assert res["ok"] is False
        assert "read_agent_result" in res["error"]

    run(flow())


def test_followup_dedup_same_content(isolated):
    async def flow():
        client = GatedClient()
        host = _host(client)
        await host.spawn(task_name="a", message="m")
        await asyncio.wait_for(client.ready.wait(), timeout=5)
        first = await host.followup("a", "同一条")
        second = await host.followup("a", "同一条")
        assert first["ok"] is True
        assert second.get("deduped") is True
        client.release.set()
        await host.wait(targets=["a"], timeout_s=10)

    run(flow())


def test_followup_limit_per_agent(isolated):
    async def flow():
        client = GatedClient()
        host = _host(client)
        await host.spawn(task_name="a", message="m")
        await asyncio.wait_for(client.ready.wait(), timeout=5)
        oks = [await host.followup("a", f"第{i}条") for i in range(7)]
        assert sum(1 for r in oks if r["ok"]) == 5
        assert any(not r["ok"] for r in oks[5:])
        client.release.set()
        await host.wait(targets=["a"], timeout_s=10)

    run(flow())


def test_followup_unknown_target(isolated):
    async def flow():
        host = _host(GatedClient())
        res = await host.followup("nope", "x")
        assert res["ok"] is False
        assert "未知子代理" in res["error"]

    run(flow())


def test_followup_task_tool_is_registered():
    from app.subagent.tools import build_subagent_tools

    names = [t.name for t in build_subagent_tools(None)]
    assert "followup_task" in names


def test_followup_task_tool_returns_json(isolated):
    async def flow():
        host = _host(GatedClient())
        tools = {t.name: t for t in __import__("app.subagent.tools", fromlist=["x"]).build_subagent_tools(host)}
        out = await tools["followup_task"].call({"target": "nope", "message": "x"})
        data = json.loads(out)
        assert data["ok"] is False
        assert "未知子代理" in data["error"]

    run(flow())


def test_followup_emits_visible_subagent_event(isolated):
    async def flow():
        client = GatedClient()
        buf = EventBuffer()
        host = SubagentHost(_ctx(), parent_runtime=Parent(client), buffer=buf)
        await host.spawn(task_name="a", message="m")
        await asyncio.wait_for(client.ready.wait(), timeout=5)
        await host.followup("a", "可见的追加指令")
        client.release.set()
        await host.wait(targets=["a"], timeout_s=10)

        seen = []
        buf.close()
        async for ev in buf.stream():
            if ev.get("type") == "subagent_event":
                seen.append(ev.get("event", {}).get("type"))
        assert "message_injected" in seen

    run(flow())
