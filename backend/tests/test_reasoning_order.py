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
