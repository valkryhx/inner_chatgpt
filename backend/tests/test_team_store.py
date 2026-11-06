"""Owner-scope and CAS contract for durable Agent Team facts."""
from __future__ import annotations

import asyncio

import pytest

from app.scope import TeamScope
from app import team_store


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "team.db"
    monkeypatch.setattr(team_store, "DB_PATH", path)
    run(team_store.init_tables())
    return path


def scope(uid="u1", sid="s1", gid="g1", tid="t1", agent="root", role="lead"):
    return TeamScope(uid, sid, gid, tid, agent, role)


def test_generation_lease_is_single_active(isolated_db):
    async def flow():
        assert await team_store.acquire_generation("u1", "s1", "g1") is True
        assert await team_store.acquire_generation("u1", "s1", "g2") is False
        assert await team_store.get_active_generation("u1", "s1") == "g1"
        await team_store.release_generation("u1", "s1", "g1")
        assert await team_store.get_active_generation("u1", "s1") is None
        assert await team_store.acquire_generation("u1", "s1", "g2") is True
        assert await team_store.acquire_generation("u2", "s1", "g9") is True
    run(flow())


def test_task_cas_and_scope(isolated_db):
    async def flow():
        s = scope()
        await team_store.create_team(s, name="demo")
        task = await team_store.create_task(s, subject="research")
        assert task["version"] == 0 and task["status"] == "pending"
        claimed = await team_store.claim_task(s, task["task_id"], expected_version=0)
        assert claimed["ok"] is True and claimed["task"]["owner_agent_id"] == "root"
        # stale expected version loses
        lost = await team_store.complete_task(s, task["task_id"], expected_version=0)
        assert lost["ok"] is False
        done = await team_store.complete_task(s, task["task_id"], expected_version=1)
        assert done["ok"] is True and done["task"]["status"] == "completed"
        # another owner/user cannot see it
        assert await team_store.get_task(scope(uid="u2"), task["task_id"]) is None
        assert await team_store.list_tasks(scope(uid="u2")) == []
        assert await team_store.get_task(scope(gid="g2"), task["task_id"]) is None
    run(flow())


def test_teammate_cannot_complete_others_task(isolated_db):
    async def flow():
        lead = scope(agent="root")
        await team_store.create_team(lead, name="demo")
        task = await team_store.create_task(lead, subject="x")
        bound = await team_store.bind_task_owner(lead, task["task_id"], "sub:a", expected_version=0)
        assert bound["ok"] is True
        other = TeamScope("u1", "s1", "g1", "t1", "sub:b", "teammate")
        out = await team_store.complete_task(other, task["task_id"], expected_version=1)
        assert out["ok"] is False
        owner = TeamScope("u1", "s1", "g1", "t1", "sub:a", "teammate")
        ok = await team_store.complete_task(owner, task["task_id"], expected_version=1)
        assert ok["ok"] is True
    run(flow())


def test_dependency_blocks_claim_and_unlocks(isolated_db):
    async def flow():
        s = scope()
        await team_store.create_team(s, name="demo")
        a = await team_store.create_task(s, subject="a")
        b = await team_store.create_task(s, subject="b", blocked_by=[a["task_id"]])
        blocked = await team_store.claim_task(s, b["task_id"], expected_version=0)
        assert blocked["reason"] == "blocked"
        assert (await team_store.claim_task(s, a["task_id"], expected_version=0))["ok"]
        assert (await team_store.complete_task(s, a["task_id"], expected_version=1))["ok"]
        assert (await team_store.claim_task(s, b["task_id"], expected_version=0))["ok"]
    run(flow())


def test_cross_team_dependency_rejected(isolated_db):
    async def flow():
        a = scope(tid="t1")
        b = scope(tid="t2")
        await team_store.create_team(a, name="a")
        await team_store.create_team(b, name="b")
        task_a = await team_store.create_task(a, subject="a")
        with pytest.raises(ValueError):
            await team_store.create_task(b, subject="b", blocked_by=[task_a["task_id"]])
    run(flow())


def test_event_cursor_monotonic_and_scoped(isolated_db):
    async def flow():
        s = scope()
        await team_store.create_team(s, name="demo")
        task = await team_store.create_task(s, subject="e")
        await team_store.claim_task(s, task["task_id"], expected_version=0)
        events = await team_store.events_since(s, 0)
        seqs = [e["event_seq"] for e in events]
        assert seqs == sorted(seqs) and len(seqs) >= 3
        assert await team_store.events_since(scope(uid="u2"), 0) == []
        after = await team_store.events_since(s, seqs[0])
        assert all(e["event_seq"] > seqs[0] for e in after)
    run(flow())


def test_concurrent_event_sequences_are_unique(isolated_db):
    async def flow():
        s = scope()
        await team_store.create_team(s, name="demo")

        async def emit(i):
            return await team_store.emit_event(s, "note", {"i": i})

        rows = await asyncio.gather(*(emit(i) for i in range(20)))
        assert len({r["event_seq"] for r in rows}) == 20
        events = await team_store.events_since(s, 0, limit=100)
        seqs = [e["event_seq"] for e in events]
        assert len(seqs) == len(set(seqs))
    run(flow())


def test_restart_recovery_releases_stale_leases_and_tasks(isolated_db):
    async def flow():
        s = scope()
        assert await team_store.acquire_generation("u1", "s1", "g1") is True
        await team_store.create_team(s, name="demo")
        task = await team_store.create_task(s, subject="x")
        claimed = await team_store.claim_task(s, task["task_id"], expected_version=0)
        assert claimed["ok"] is True
        counts = await team_store.recover_stale_runtime("test")
        assert counts == {"generations": 1, "tasks": 1, "teams": 1}
        assert await team_store.get_active_generation("u1", "s1") is None
        assert await team_store.acquire_generation("u1", "s1", "g2") is True
        row = await team_store.get_task(s, task["task_id"])
        assert row is not None and row["status"] == "pending" and row["version"] == 2
        team = await team_store.get_team(s)
        assert team is not None and team["status"] == "stale"
    run(flow())


def test_create_team_resumes_open_board_across_generations(isolated_db):
    async def flow():
        first = scope(gid="g1", tid="t1")
        await team_store.acquire_generation("u1", "s1", "g1")
        await team_store.create_team(first, name="first")
        a = await team_store.create_task(first, subject="draft")
        b = await team_store.create_task(first, subject="review", blocked_by=[a["task_id"]])
        assert (await team_store.claim_task(first, a["task_id"], expected_version=0))["ok"]
        await team_store.send_message(
            first, recipient="lead", kind="note", content="draft is ready"
        )
        await team_store.set_team_status(first, "stale")

        second = scope(gid="g2", tid="t2")
        await team_store.acquire_generation("u1", "s1", "g2")
        await team_store.create_team(second, name="second")
        tasks = await team_store.list_tasks(second)
        assert len(tasks) == 2
        resumed = {t["subject"]: t for t in tasks}
        assert resumed["draft"]["status"] == "pending"
        assert resumed["draft"]["owner_agent_id"] == ""
        assert resumed["review"]["status"] == "pending"
        assert resumed["review"]["blockedBy"] == [resumed["draft"]["task_id"]]
        assert all(t["task_id"] not in (a["task_id"], b["task_id"]) for t in tasks)
        messages = await team_store.list_messages(second)
        assert [m["content"] for m in messages] == ["draft is ready"]
        events = await team_store.events_since(second, 0)
        resume_event = next(e for e in events if e["event_type"] == "team_resumed")
        assert resume_event["payload"]["source_team_id"] == "t1"
        assert resume_event["payload"]["source_generation_id"] == "g1"
        assert resume_event["payload"]["open_task_count"] == 2
        assert resume_event["payload"]["message_count"] == 1
        # The source board remains an immutable audit snapshot.
        old_a = await team_store.get_task(first, a["task_id"])
        assert old_a is not None and old_a["status"] == "in_progress"

    run(flow())


def test_resume_does_not_cross_user_or_copy_completed_only(isolated_db):
    async def flow():
        first = scope(uid="u1", sid="s1", gid="g1", tid="t1")
        await team_store.create_team(first, name="first")
        done = await team_store.create_task(first, subject="done")
        assert (await team_store.claim_task(first, done["task_id"], expected_version=0))["ok"]
        assert (await team_store.complete_task(first, done["task_id"], expected_version=1))["ok"]
        await team_store.set_team_status(first, "stale")

        other = scope(uid="u2", sid="s1", gid="g2", tid="t2")
        await team_store.create_team(other, name="other")
        assert await team_store.list_tasks(other) == []

        second = scope(uid="u1", sid="s1", gid="g2", tid="t3")
        await team_store.create_team(second, name="second")
        assert await team_store.list_tasks(second) == []
        assert not any(
            e["event_type"] == "team_resumed"
            for e in await team_store.events_since(second, 0)
        )

    run(flow())
