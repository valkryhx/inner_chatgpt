"""Self-built kernel unit tests (no network, no langchain/langgraph).

Run: cd backend && python -m pytest tests -q
"""
from __future__ import annotations

import asyncio
import json

from app.agent_loop import AgentLoop, _safe_json
from app.sse import detect_artifacts
from app.tokens import estimate_tokens, is_summary_message, message_text
from app.tools import Tool, ToolRegistry


def run(coro):
    return asyncio.run(coro)


# ---------------- tools.ToolRegistry ----------------
def test_registry_schema_shape():
    async def call(args):
        return "ok"

    reg = ToolRegistry([Tool("t1", "desc", {"type": "object", "properties": {"a": {"type": "string"}}}, call)])
    schemas = reg.schemas()
    assert schemas[0]["type"] == "function"
    assert schemas[0]["function"]["name"] == "t1"
    assert schemas[0]["function"]["parameters"]["properties"]["a"]["type"] == "string"


def test_registry_dispatch_never_raises():
    async def boom(args):
        raise ValueError("bad")

    reg = ToolRegistry([Tool("t1", "", {"type": "object", "properties": {}}, boom)])
    assert "未知工具" in run(reg.dispatch("missing", {}))
    out = run(reg.dispatch("t1", {}))
    assert out.startswith("[工具执行错误]")
    assert "ValueError" in out


def test_registry_normalizes_non_dict_args():
    async def call(args):
        return json.dumps(args)

    reg = ToolRegistry([Tool("t1", "", {"type": "object", "properties": {}}, call)])
    assert run(reg.dispatch("t1", None)) == "{}"


# ---------------- agent_loop.sanitize ----------------
def test_sanitize_replays_missing_known_tool():
    async def ok(args):
        return "OK"

    loop = AgentLoop(client=None, registry=ToolRegistry([Tool("good", "", {}, ok)]), system_prompt="")
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "good", "arguments": "{}"}}]},
    ]
    changed = run(loop.sanitize(history))
    assert changed is True
    assert history[-1] == {"role": "tool", "tool_call_id": "c1", "name": "good", "content": "OK"}


def test_sanitize_drops_unknown_dangling_tool_call():
    loop = AgentLoop(client=None, registry=ToolRegistry([]), system_prompt="")
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c9", "type": "function", "function": {"name": "missing", "arguments": "{}"}}]},
    ]
    assert run(loop.sanitize(history)) is True
    assert history == [{"role": "user", "content": "hi"}]


def test_sanitize_noop_when_paired():
    loop = AgentLoop(client=None, registry=ToolRegistry([]), system_prompt="")
    history = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "x", "content": "1"},
    ]
    assert run(loop.sanitize(history)) is False


def test_safe_json():
    assert _safe_json('{"a":1}') == {"a": 1}
    assert _safe_json("[1]") == {"_raw": "[1]"}
    assert _safe_json("") == {}
    assert _safe_json({"b": 2}) == {"b": 2}


# ---------------- sse.detect_artifacts ----------------
def test_detect_artifacts_plain_and_nested():
    assert detect_artifacts('{"type":"chart","spec":{"a":1}}') == [{"type": "chart", "spec": {"a": 1}}]
    nested = json.dumps([{"type": "text", "text": json.dumps({"type": "chart", "x": 1})}])
    assert detect_artifacts(nested) == [{"type": "chart", "x": 1}]
    assert detect_artifacts("plain text") == []
    assert detect_artifacts("") == []


# ---------------- tokens ----------------
def test_tokens_cjk_aware_and_summary():
    assert estimate_tokens("你好世界") >= 4
    assert estimate_tokens("") == 0
    assert is_summary_message({"role": "user", "_source": "summarization"}) is True
    assert is_summary_message({"role": "user"}) is False
    assert message_text({"content": [{"type": "text", "text": "a"}, {"type": "reasoning", "text": "b"}]}) == "a b"


# ---------------- history_store (isolated temp db) ----------------
def test_history_store_crud(tmp_path, monkeypatch):
    import app.history_store as hs

    monkeypatch.setattr(hs, "DB_PATH", tmp_path / "t.db")
    store = hs.HistoryStore()

    async def scenario():
        await store.init()
        assert await store.get("s1") == []
        await store.set("s1", [{"role": "user", "content": "hi"}])
        assert (await store.get("s1"))[0]["content"] == "hi"
        full = await store.append("s1", [{"role": "assistant", "content": "yo"}])
        assert [m["role"] for m in full] == ["user", "assistant"]
        assert await store.exists("s1") is True
        await store.delete("s1")
        assert await store.exists("s1") is False

    run(scenario())


# ---------------- sse.run_agent_stream: artifact -> chart event ----------------
def test_run_agent_stream_emits_chart_artifact():
    from app.sse import run_agent_stream

    class FakeStore:
        def __init__(self):
            self.history = []

        async def get(self, sid):
            return list(self.history)

        async def set(self, sid, hist):
            self.history = list(hist)

    class FakeLoop:
        async def run_stream(self, history, stop_event=None, session_id=""):
            yield {"type": "tool_start", "id": "c1", "name": "chart_tool", "input": {}}
            yield {"type": "tool_result", "id": "c1", "name": "chart_tool",
                   "content": json.dumps({"type": "chart", "spec": {"series": []}}), "is_error": False}
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "done", "status": "complete"}

    class FakeUR:
        store = FakeStore()
        loop = FakeLoop()

    async def scenario():
        out = []
        async for ev in run_agent_stream(FakeUR(), "s1", "hi"):
            out.append(ev)
        return out

    events = run(scenario())
    types = [e["type"] for e in events]
    assert types == ["tool_start", "tool_result", "chart", "text_delta", "done"]
    # contract: the whole artifact object is forwarded as `spec` (frontend reads spec.chart_type/option/...)
    assert events[2]["spec"] == {"type": "chart", "spec": {"series": []}}
    # user message must be appended to history before the loop starts
    assert FakeUR.store.history[0] == {"role": "user", "content": "hi"}


# ---------------- compaction: never split assistant/tool pair ----------------
def test_safe_keep_from_skips_tool_boundary():
    from app.compaction import _safe_keep_from

    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    # keep_from pointing at the tool msg must back off to the assistant that owns it
    assert _safe_keep_from(msgs, 2) == 1
    assert _safe_keep_from(msgs, 0) == 0
    assert _safe_keep_from(msgs, 4) == 4


def test_history_store_legacy_migration_backfills_owner(tmp_path, monkeypatch):
    import asyncio
    import sqlite3
    import app.history_store as hs

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL);
        CREATE TABLE session_history (
            session_id TEXT PRIMARY KEY,
            history TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        INSERT INTO sessions VALUES ('s1', 'u1');
        INSERT INTO session_history VALUES ('s1', '[{"role":"user","content":"old"}]', 1);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(hs, "DB_PATH", db)
    store = hs.HistoryStore()

    async def scenario():
        await store.init()
        assert (await store.get("u1", "s1"))[0]["content"] == "old"
        assert await store.get("other", "s1") == []

    asyncio.run(scenario())
