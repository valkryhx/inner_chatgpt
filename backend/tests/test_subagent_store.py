"""S1: AgentContext / sidechain store / event buffer."""
from __future__ import annotations

import asyncio

import pytest

from app.subagent.context import AgentContext
from app.subagent import store as sub_store
from app.subagent.events import EventBuffer, StopSignal


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "sub.db"
    monkeypatch.setattr(sub_store, "DB_PATH", db_path)
    run(sub_store.init_tables())
    return db_path


# ---------------- context ----------------
def test_agent_context_child_derives_identity():
    root = AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")
    assert root.agent_id == "root" and root.depth == 0 and not root.is_subagent
    child = root.child("scan")
    assert child.agent_id == "sub:scan"
    assert child.parent_agent_id == "root"
    assert child.depth == 1 and child.is_subagent
    # identity must be carried unchanged
    assert (child.user_id, child.session_id, child.message_id, child.generation_id) == ("u1", "s1", "m1", "g1")
    grand = child.child("deep")
    assert grand.depth == 2 and grand.parent_agent_id == "sub:scan"


def test_agent_context_is_frozen():
    root = AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")
    with pytest.raises(Exception):
        root.user_id = "u2"  # type: ignore[misc]


# ---------------- store ----------------
def test_run_lifecycle_and_ownership(isolated_db):
    async def flow():
        row = await sub_store.create_run(
            run_id="run_1", ctx_user_id="u1", session_id="s1", message_id="m1",
            parent_agent_id="root", agent_id="sub:scan", name="scan", role="general",
            task_message="看看代码",
        )
        assert row["status"] == "running"
        await sub_store.finish_run("run_1", status="completed", result_text="结论", turns=3)
        got = await sub_store.get_run("run_1")
        assert got["status"] == "completed"
        assert got["result_text"] == "结论"
        assert got["turns"] == 3
        assert got["finished_at"] > 0

        # 所有权：其他用户/会话查不到
        assert await sub_store.list_runs("s1", "u1") == [got] or len(await sub_store.list_runs("s1", "u1")) == 1
        assert await sub_store.list_runs("s1", "u2") == []
        assert await sub_store.list_runs("s2", "u1") == []

    run(flow())


def test_sidechain_append_is_incremental(isolated_db):
    async def flow():
        await sub_store.create_run(
            run_id="run_2", ctx_user_id="u1", session_id="s1", message_id="m1",
            parent_agent_id="root", agent_id="sub:a", name="a", role="general",
            task_message="t",
        )
        history = [{"role": "user", "content": "hi"}]
        seq = await sub_store.append_sidechain("run_2", history, start_seq=0)
        assert seq == 1
        history.append({"role": "assistant", "content": "yo"})
        seq = await sub_store.append_sidechain("run_2", history, start_seq=seq)
        assert seq == 2
        # 重复调用同一 start_seq 不应重复
        seq2 = await sub_store.append_sidechain("run_2", history, start_seq=seq)
        assert seq2 == 2
        loaded = await sub_store.load_sidechain("run_2")
        assert [m["content"] for m in loaded] == ["hi", "yo"]

    run(flow())


def test_stale_running_marked_errored(isolated_db):
    async def flow():
        await sub_store.create_run(
            run_id="run_3", ctx_user_id="u1", session_id="s1", message_id="m1",
            parent_agent_id="root", agent_id="sub:b", name="b", role="general",
            task_message="t",
        )
        n = await sub_store.mark_stale_running_as_errored()
        assert n == 1
        got = await sub_store.get_run("run_3")
        assert got["status"] == "errored"
        assert "interrupted" in got["error"]

    run(flow())


# ---------------- events ----------------
def test_stop_signal():
    sig = StopSignal()
    assert not sig.is_set()
    sig.set()
    assert sig.is_set()


def test_event_buffer_order_and_close():
    async def flow():
        buf = EventBuffer()
        buf.put({"type": "a"})
        buf.put({"type": "b"})
        buf.close()
        got = [ev["type"] async for ev in buf.stream()]
        assert got == ["a", "b"]

    run(flow())
