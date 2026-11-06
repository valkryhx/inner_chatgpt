"""Regression coverage for stop/cancel and stale generation-lease recovery."""
from __future__ import annotations

import asyncio

import pytest

from app import runtime, team_store
from app.agent_loop import AgentLoop
from app.scope import TeamScope
from app.tools import ToolRegistry


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated_team_db(tmp_path, monkeypatch):
    path = tmp_path / "team.db"
    monkeypatch.setattr(team_store, "DB_PATH", path)
    run(team_store.init_tables())
    return path


def test_cancel_generation_cancels_registered_streaming_task():
    async def scenario():
        started = asyncio.Event()

        async def worker():
            runtime.register_generation("u1", "s1", "g1")
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                runtime.unregister_generation("u1", "s1", "g1")

        task = asyncio.create_task(worker())
        await asyncio.wait_for(started.wait(), timeout=1)
        assert runtime.is_generation_live("u1", "s1", "g1") is True
        assert runtime.cancel_generation("u1", "s1", "g1") == 1
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.is_generation_live("u1", "s1", "g1") is False

    run(scenario())


def test_stop_before_stream_registration_is_not_lost():
    async def scenario():
        assert runtime.request_stop("u2", "s2", "g2") == 0
        event = runtime.register_generation("u2", "s2", "g2")
        assert event.is_set() is True
        runtime.unregister_generation("u2", "s2", "g2")

    run(scenario())


def test_prestream_stop_is_observed_by_first_registration():
    """A stop may arrive during the request hand-off, before guarded_stream
    captures its task. The next register_generation() must return a set event."""
    async def scenario():
        runtime.request_stop("u4", "s4", "g4")
        event = runtime.register_generation("u4", "s4", "g4")
        assert event.is_set() is True
        runtime.unregister_generation("u4", "s4", "g4")

    run(scenario())


def test_reservation_expires_so_it_cannot_block_forever(monkeypatch):
    runtime.reserve_generation("u3", "s3", "g3")
    key = ("u3", "s3", "g3")
    assert runtime.is_generation_live(*key) is True
    monkeypatch.setitem(
        runtime._generation_reservations, key,
        runtime.time.monotonic() - runtime._RESERVATION_TTL_S - 1,
    )
    assert runtime.has_active_generation("s3", "u3") is False
    assert key not in runtime._generation_reservations


def test_converge_generation_releases_team_and_invalidates_owner_cas(isolated_team_db):
    async def scenario():
        scope = TeamScope("u1", "s1", "g1", "t1", "root", "lead")
        assert await team_store.acquire_generation("u1", "s1", "g1") is True
        await team_store.create_team(scope, name="demo")
        task = await team_store.create_task(scope, subject="write doc")
        claimed = await team_store.claim_task(scope, task["task_id"], expected_version=0)
        assert claimed["ok"] is True

        counts = await team_store.converge_generation_facts(
            "u1", "s1", "g1", status="stopped"
        )
        assert counts == {"generations": 1, "tasks": 1, "teams": 1}
        assert await team_store.get_active_generation("u1", "s1") is None
        row = await team_store.get_task(scope, task["task_id"])
        assert row is not None
        assert row["status"] == "pending" and row["owner_agent_id"] == ""
        assert row["version"] == 2
        team = await team_store.get_team(scope)
        assert team is not None and team["status"] == "stopped"

    run(scenario())


def test_stop_endpoint_cancels_task_and_converges_lease(monkeypatch):
    from app.routers import chat
    from app.schemas import StopIn

    async def scenario():
        started = asyncio.Event()

        async def worker():
            runtime.register_generation("u1", "s1", "g1")
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                runtime.unregister_generation("u1", "s1", "g1")

        task = asyncio.create_task(worker())
        await asyncio.wait_for(started.wait(), timeout=1)

        async def fake_get_session(sid, uid):
            return {"id": sid, "user_id": uid}

        async def fake_list_messages(sid):
            return [{
                "id": "m1", "session_id": sid, "role": "assistant",
                "content": "已经写出的内容", "reasoning": "",
                "blocks": [{"type": "text", "text": "已经写出的内容"}],
                "status": "stopped" if task.done() else "streaming", "created_at": 1,
            }]

        async def fake_update_message(mid, **fields):
            return None

        async def fake_mark_stopped(sid):
            return 0

        converged = []
        released = []
        killed = []

        async def fake_converge(uid, sid, gid, *, status="stale"):
            converged.append((uid, sid, gid, status))
            return {"generations": 1, "tasks": 0, "teams": 0}

        async def fake_release(uid, sid, gid):
            released.append((uid, sid, gid))

        async def fake_stop_code(uid, sid, gid):
            killed.append((uid, sid, gid))

        monkeypatch.setattr(chat, "get_hub", lambda: None)
        monkeypatch.setattr(chat, "stop_code_runs", fake_stop_code)
        monkeypatch.setattr(chat.db, "get_session", fake_get_session)
        monkeypatch.setattr(chat.db, "list_messages", fake_list_messages)
        monkeypatch.setattr(chat.db, "update_message", fake_update_message)
        monkeypatch.setattr(chat.db, "mark_stale_streaming_stopped", fake_mark_stopped)
        monkeypatch.setattr(team_store, "converge_generation_facts", fake_converge)
        monkeypatch.setattr(team_store, "release_generation", fake_release)

        out = await chat.stop_generation(
            "s1", StopIn(generation_id="g1", message_id="m1"), {"id": "u1"}
        )
        assert out.ok is True
        assert out.message is not None and out.message.status == "stopped"
        with pytest.raises(asyncio.CancelledError):
            await task
        assert converged == [("u1", "s1", "g1", "stopped")]
        assert released == [("u1", "s1", "g1")]
        assert killed == [("u1", "s1", "g1")]
        assert runtime.is_generation_live("u1", "s1", "g1") is False

    run(scenario())


def test_send_message_self_heals_an_orphaned_db_lease(monkeypatch):
    from app.routers import chat
    from app.schemas import SendMessageIn

    async def scenario():
        calls = {"acquire": 0, "converge": 0}

        async def fake_get_session(sid, uid):
            return {"id": sid, "user_id": uid, "title": "x", "model_id": ""}

        async def fake_acquire(uid, sid, gid):
            calls["acquire"] += 1
            return calls["acquire"] == 2

        async def fake_active(uid, sid):
            return "g_old"

        async def fake_converge(uid, sid, gid, *, status="stale"):
            calls["converge"] += 1
            assert (uid, sid, gid, status) == ("u1", "s1", "g_old", "stale")
            return {"generations": 1, "tasks": 0, "teams": 0}

        async def fake_mark(sid):
            return 0

        async def fake_count(sid):
            return 1

        monkeypatch.setattr(chat.db, "get_session", fake_get_session)
        monkeypatch.setattr(chat.db, "mark_stale_streaming_stopped", fake_mark)
        monkeypatch.setattr(chat.db, "count_messages", fake_count)
        monkeypatch.setattr(chat, "is_generation_live", lambda uid, sid, gid: False)
        monkeypatch.setattr(team_store, "acquire_generation", fake_acquire)
        monkeypatch.setattr(team_store, "get_active_generation", fake_active)
        monkeypatch.setattr(team_store, "converge_generation_facts", fake_converge)

        response = await chat.send_message(
            "s1", SendMessageIn(text="继续", generation_id="g_new"), {"id": "u1"}
        )
        assert response.status_code == 200
        assert calls["acquire"] == 2
        assert calls["converge"] == 1
        runtime.release_generation_reservation("u1", "s1", "g_new")

    run(scenario())


def test_agent_loop_cancellation_stops_inner_llm_call():
    class SlowClient:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def run(self, *args, **kwargs):
            self.started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    async def scenario():
        client = SlowClient()
        loop = AgentLoop(client, ToolRegistry([]), "sys")

        async def consume():
            async for _ in loop.run_stream([{"role": "user", "content": "hi"}]):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(client.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.cancelled.is_set() is True

    run(scenario())
