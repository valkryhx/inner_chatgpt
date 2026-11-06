"""Regression: thinking (reasoning) deltas must keep true event order as blocks.

Bug: thinking_delta only accumulated into msg.reasoning and was rendered after all
blocks, so the "thinking" section always appeared at the bottom of the message.
Fix: reasoning deltas become ordered `reasoning` blocks interleaved with tool/text.
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _collect_frames(gen):
    async def _run():
        out = []
        async for frame in gen:
            out.append(frame)
        return out
    return asyncio.run(_run())


def test_reasoning_kept_in_event_order(monkeypatch):
    from app.routers import chat
    from app import db

    captured: dict = {}

    async def fake_add_message(*a, **k):
        return {"id": k.get("message_id", "m")}

    async def fake_update_message(message_id, **fields):
        captured["content"] = fields.get("content")
        captured["reasoning"] = fields.get("reasoning")
        captured["blocks"] = fields.get("blocks")
        captured["status"] = fields.get("status")

    async def fake_run_agent_stream(ur, session_id, text, stop_event=None):
        yield {"type": "thinking_delta", "text": "先想一下"}
        yield {"type": "tool_start", "id": "c1", "name": "code_run", "input": {"code": "1+1"}}
        yield {"type": "tool_result", "id": "c1", "name": "code_run", "content": "2", "is_error": False}
        yield {"type": "thinking_delta", "text": "再想一下"}
        yield {"type": "text_delta", "text": "结果=2"}
        yield {"type": "done", "status": "complete"}

    class FakeModel:
        model_name = "fake"

    class FakeUR:
        model = FakeModel()

        async def sanitize_pending_tool_calls(self, sid):
            return False

    class FakeHub:
        async def get_user_runtime(self, uid, mid=""):
            return FakeUR()

    monkeypatch.setattr(chat, "get_hub", lambda: FakeHub())
    monkeypatch.setattr(chat, "run_agent_stream", fake_run_agent_stream)
    monkeypatch.setattr(db, "add_message", fake_add_message)
    monkeypatch.setattr(db, "update_message", fake_update_message)
    # compaction is wrapped in try/except; make it a no-op to avoid DB access
    import app.compaction as compaction
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(compaction, "enforce_soft_limit", _noop, raising=False)
    monkeypatch.setattr(compaction, "enforce_hard_limit", _noop, raising=False)

    _collect_frames(chat._event_stream("u1", "s1", "hi", "g1", "m1"))

    blocks = captured["blocks"]
    types = [b["type"] for b in blocks]
    assert types == ["reasoning", "tool", "reasoning", "text"], types
    assert blocks[0]["text"] == "先想一下"
    assert blocks[2]["text"] == "再想一下"
    # reasoning string is still fully accumulated (for persistence/back-compat)
    assert captured["reasoning"] == "先想一下再想一下"
    assert captured["content"] == "结果=2"


def test_reasoning_consecutive_chunks_merge(monkeypatch):
    from app.routers import chat
    from app import db

    captured: dict = {}

    async def fake_add_message(*a, **k):
        return {"id": "m"}

    async def fake_update_message(message_id, **fields):
        captured["blocks"] = fields.get("blocks")

    async def fake_run_agent_stream(ur, session_id, text, stop_event=None):
        yield {"type": "thinking_delta", "text": "A"}
        yield {"type": "thinking_delta", "text": "B"}
        yield {"type": "text_delta", "text": "ans"}
        yield {"type": "done", "status": "complete"}

    class FakeUR:
        class model:
            model_name = "fake"

        async def sanitize_pending_tool_calls(self, sid):
            return False

    class FakeHub:
        async def get_user_runtime(self, uid, mid=""):
            return FakeUR()

    monkeypatch.setattr(chat, "get_hub", lambda: FakeHub())
    monkeypatch.setattr(chat, "run_agent_stream", fake_run_agent_stream)
    monkeypatch.setattr(db, "add_message", fake_add_message)
    monkeypatch.setattr(db, "update_message", fake_update_message)
    import app.compaction as compaction
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(compaction, "enforce_soft_limit", _noop, raising=False)
    monkeypatch.setattr(compaction, "enforce_hard_limit", _noop, raising=False)

    _collect_frames(chat._event_stream("u1", "s1", "hi", "g1", "m1"))
    types = [b["type"] for b in captured["blocks"]]
    assert types == ["reasoning", "text"], types
    assert captured["blocks"][0]["text"] == "AB"


def test_stop_persists_partial_text_before_done(monkeypatch):
    """Stopping after visible deltas must persist those deltas as a stopped snapshot."""
    from app.routers import chat
    from app import db

    snapshots = []

    async def fake_add_message(*a, **k):
        return {"id": k.get("message_id", "m")}

    async def fake_update_message(message_id, **fields):
        snapshots.append(dict(fields))

    async def fake_run_agent_stream(ur, session_id, text, stop_event=None):
        yield {"type": "text_delta", "text": "已经生成的前半段"}
        raise asyncio.CancelledError()

    class FakeUR:
        class model:
            model_name = "fake"

        async def sanitize_pending_tool_calls(self, sid):
            return False

    class FakeHub:
        async def get_user_runtime(self, uid, mid=""):
            return FakeUR()

    monkeypatch.setattr(chat, "get_hub", lambda: FakeHub())
    monkeypatch.setattr(chat, "run_agent_stream", fake_run_agent_stream)
    monkeypatch.setattr(db, "add_message", fake_add_message)
    monkeypatch.setattr(db, "update_message", fake_update_message)
    import app.compaction as compaction

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(compaction, "enforce_soft_limit", _noop, raising=False)
    monkeypatch.setattr(compaction, "enforce_hard_limit", _noop, raising=False)

    with pytest.raises(asyncio.CancelledError):
        _collect_frames(chat._event_stream("u1", "s1", "hi", "g1", "m1"))

    assert snapshots, "stop path must persist a snapshot"
    final = snapshots[-1]
    assert final["status"] == "stopped"
    assert final["content"] == "已经生成的前半段"
    assert [b["type"] for b in final["blocks"]] == ["text"]
    assert final["blocks"][0]["text"] == "已经生成的前半段"


def test_stop_result_returns_persisted_snapshot(monkeypatch):
    from app.routers import chat
    from app import db
    from app.schemas import StopIn

    async def fake_get_session(sid, uid):
        return {"id": sid, "user_id": uid}

    async def fake_list_messages(sid):
        return [{"id": "m1", "session_id": sid, "role": "assistant",
                 "content": "保留我", "reasoning": "",
                 "blocks": [{"type": "text", "text": "保留我"}],
                 "status": "stopped", "created_at": 1}]

    monkeypatch.setattr(chat, "request_stop", lambda sid, gid: 1)
    monkeypatch.setattr(chat, "get_hub", lambda: None)
    monkeypatch.setattr(db, "get_session", fake_get_session)
    monkeypatch.setattr(db, "list_messages", fake_list_messages)

    out = asyncio.run(chat.stop_generation(
        "s1", StopIn(generation_id="g1", message_id="m1"), {"id": "u1"}
    ))
    assert out.ok is True
    assert out.message is not None
    assert out.message.content == "保留我"
    assert out.message.status == "stopped"


def test_stop_endpoint_releases_generation_lease(monkeypatch):
    """A successful stop must not leave the next turn blocked by a stale lease."""
    from app.routers import chat
    from app import db, team_store
    from app.schemas import StopIn

    released: list[tuple[str, str, str]] = []

    async def fake_get_session(sid, uid):
        return {"id": sid, "user_id": uid}

    async def fake_release(uid, sid, gid):
        released.append((uid, sid, gid))

    monkeypatch.setattr(chat, "request_stop", lambda uid, sid, gid: 1)
    monkeypatch.setattr(chat, "get_hub", lambda: None)
    monkeypatch.setattr(db, "get_session", fake_get_session)
    monkeypatch.setattr(team_store, "release_generation", fake_release)

    out = asyncio.run(chat.stop_generation(
        "s1", StopIn(generation_id="g1"), {"id": "u1"}
    ))
    assert out.ok is True
    assert released == [("u1", "s1", "g1")]


def _wire_event_stream(monkeypatch, fake_run_agent_stream, captured):
    """把 _event_stream 的外部依赖收窄到内存假实现。"""
    from app.routers import chat
    from app import db

    async def fake_add_message(*a, **k):
        return {"id": k.get("message_id", "m")}

    async def fake_update_message(message_id, **fields):
        captured.append(dict(fields))

    class FakeUR:
        class model:
            model_name = "fake"

        async def sanitize_pending_tool_calls(self, sid):
            return False

    class FakeHub:
        async def get_user_runtime(self, uid, mid=""):
            return FakeUR()

    monkeypatch.setattr(chat, "get_hub", lambda: FakeHub())
    monkeypatch.setattr(chat, "run_agent_stream", fake_run_agent_stream)
    monkeypatch.setattr(db, "add_message", fake_add_message)
    monkeypatch.setattr(db, "update_message", fake_update_message)
    import app.compaction as compaction

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(compaction, "enforce_soft_limit", _noop, raising=False)
    monkeypatch.setattr(compaction, "enforce_hard_limit", _noop, raising=False)
    return chat


def test_complete_terminal_status_is_not_overwritten(monkeypatch):
    snapshots = []

    async def fake_run_agent_stream(ur, session_id, text, stop_event=None):
        yield {"type": "text_delta", "text": "完整回复"}
        yield {"type": "done", "status": "complete"}

    chat = _wire_event_stream(monkeypatch, fake_run_agent_stream, snapshots)
    _collect_frames(chat._event_stream("u1", "s1", "hi", "g1", "m1"))

    assert snapshots[-1]["status"] == "complete"
    assert snapshots[-1]["content"] == "完整回复"


def test_upstream_error_terminal_status_is_not_overwritten(monkeypatch):
    snapshots = []

    async def fake_run_agent_stream(ur, session_id, text, stop_event=None):
        yield {"type": "text_delta", "text": "失败前内容"}
        yield {"type": "error", "error": "模型服务失败"}

    chat = _wire_event_stream(monkeypatch, fake_run_agent_stream, snapshots)
    _collect_frames(chat._event_stream("u1", "s1", "hi", "g1", "m1"))

    assert snapshots[-1]["status"] == "error"
    assert snapshots[-1]["content"] == "失败前内容"


def test_cancel_terminal_status_is_stopped(monkeypatch):
    snapshots = []

    async def fake_run_agent_stream(ur, session_id, text, stop_event=None):
        yield {"type": "text_delta", "text": "中断前内容"}
        raise asyncio.CancelledError()

    chat = _wire_event_stream(monkeypatch, fake_run_agent_stream, snapshots)
    with pytest.raises(asyncio.CancelledError):
        _collect_frames(chat._event_stream("u1", "s1", "hi", "g1", "m1"))

    stopped = [s for s in snapshots if s["status"] == "stopped"]
    assert stopped
    assert stopped[-1]["content"] == "中断前内容"
