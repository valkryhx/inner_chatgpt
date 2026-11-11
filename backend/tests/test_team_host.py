"""TeamHost lifecycle, scope isolation and CAS behavior."""
from __future__ import annotations

import asyncio

import pytest
from contextlib import asynccontextmanager

from app import team_store
from app.scope import TeamScope
from app.team_host import TeamHost, TeamHostError


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


class FakeQuota:
    def __init__(self):
        self.calls = 0
        self.released = 0

    async def team_lease(self, sc, *, timeout_s=0.0):
        self.calls += 1
        return self

    def release(self):
        self.released += 1

    @asynccontextmanager
    async def team(self, sc, *, timeout_s=0.0):
        lease = await self.team_lease(sc, timeout_s=timeout_s)
        try:
            yield lease
        finally:
            lease.release()


def test_create_holds_quota_until_generation_finishes(isolated_db):
    async def flow():
        quota = FakeQuota()
        host = TeamHost(scope(), quota_manager=quota)
        status = await host.create("demo")
        assert status["team"]["status"] == "active"
        assert status["team"]["name"] == "demo"
        assert quota.calls == 1 and quota.released == 0
        # idempotent create does not take a second lifecycle lease
        again = await host.create("ignored")
        assert again["team"]["team_id"] == "t1"
        assert quota.calls == 1
        await host.release_lifecycle()
        assert quota.released == 1
        await host.release_lifecycle()
        assert quota.released == 1

    run(flow())


def test_cross_user_and_session_not_visible(isolated_db):
    async def flow():
        lead = TeamHost(scope(), quota_manager=FakeQuota())
        await lead.create("demo")
        assert await TeamHost(scope(uid="u2"), quota_manager=FakeQuota()).load() is None
        assert await TeamHost(scope(sid="s2"), quota_manager=FakeQuota()).load() is None

    run(flow())


def test_task_cas_conflict_and_dependency_blocked(isolated_db):
    async def flow():
        host = TeamHost(scope(), quota_manager=FakeQuota())
        await host.create("demo")
        a = await host.create_task(subject="a")
        b = await host.create_task(subject="b", blocked_by=[a["task_id"]])
        assert (await host.claim(a["task_id"], expected_version=0))["ok"] is True
        # stale CAS loses
        assert (await host.complete(a["task_id"], expected_version=0))["reason"] == "not_owner_or_conflict"
        assert (await host.claim(b["task_id"], expected_version=0))["reason"] == "blocked"
        assert (await host.complete(a["task_id"], expected_version=1))["ok"] is True
        assert (await host.claim(b["task_id"], expected_version=0))["ok"] is True

    run(flow())


def test_teammate_cannot_create_or_reclaim_others(isolated_db):
    async def flow():
        lead = TeamHost(scope(), quota_manager=FakeQuota())
        await lead.create("demo")
        t = await lead.create_task(subject="x")
        bound = await lead.bind_task_owner(t["task_id"], "sub:a", expected_version=0)
        assert bound["ok"] is True
        mate = TeamHost(scope(agent="sub:a", role="teammate"), quota_manager=FakeQuota())
        with pytest.raises(TeamHostError):
            await mate.create_task(subject="forbidden")
        assert (await mate.complete(t["task_id"], expected_version=1))["ok"] is True
        other = TeamHost(scope(agent="sub:b", role="teammate"), quota_manager=FakeQuota())
        assert (await other.complete(t["task_id"], expected_version=2))["reason"] == "not_found"

    run(flow())


def test_message_read_scope_and_star_routing(isolated_db):
    async def flow():
        lead = TeamHost(scope(), quota_manager=FakeQuota())
        await lead.create("demo")
        await lead.send_message(recipient="sub:a", content="hello")
        await lead.send_message(recipient="sub:b", content="other")
        mate = TeamHost(scope(agent="sub:a", role="teammate"), quota_manager=FakeQuota())
        rows = await mate.list_messages()
        assert [r["content"] for r in rows] == ["hello"]
        # A spoofed recipient argument cannot widen a teammate's inbox.
        rows = await mate.list_messages(recipient="sub:b")
        assert [r["content"] for r in rows] == ["hello"]
        with pytest.raises(TeamHostError):
            await mate.send_message(recipient="sub:b", content="nope")
        await mate.send_message(recipient="lead", content="done")
        assert len(await lead.list_messages(recipient="lead")) == 1
        # another user/session cannot see either side
        assert await TeamHost(scope(uid="u2"), quota_manager=FakeQuota()).list_messages() == []

    run(flow())


def test_terminal_status_emits_team_end(isolated_db):
    async def flow():
        host = TeamHost(scope(), quota_manager=FakeQuota())
        await host.create("demo")
        assert await host.set_status("completed") is True
        events = await host.events_since(0)
        assert events[-1]["event_type"] == "team_end"
        assert host.event_shape(events[-1])["status"] == "completed"

    run(flow())


def test_event_shape_compatible_with_sse_contract(isolated_db):
    async def flow():
        host = TeamHost(scope(), quota_manager=FakeQuota())
        await host.create("demo")
        events = await host.events_since(0)
        shaped = [host.event_shape(e) for e in events]
        assert shaped[0]["type"] == "team_start"
        for key in ("team_id", "session_id", "agent_id", "task_id", "updated_at"):
            assert key in shaped[0]

    run(flow())


def test_spawn_false_result_is_not_reported_as_success(isolated_db):
    async def flow():
        async def refused(**kwargs):
            return {"ok": False, "error": "slot unavailable"}
        host = TeamHost(scope(), quota_manager=FakeQuota(), spawn_fn=refused)
        await host.create("demo")
        task = await host.create_task(subject="x")
        out = await host.spawn_teammate(
            task_name="a", message="do", team_task_id=task["task_id"]
        )
        assert out["ok"] is False
        assert out["spawn"]["error"] == "slot unavailable"
        row = await host.get_task(task["task_id"])
        assert row["status"] == "pending" and row["owner_agent_id"] == ""

    run(flow())


def test_spawn_failure_reclaims_bound_task(isolated_db):
    """spawn 抛错时任务必须停在 pending 且没有 owner。

    顺序已改为「先 spawn 再绑定」：绑定发生在 spawn 成功之后，所以失败时压根
    没有产生 owner。外部可观察的不变量不变——没有幽灵 owner——只是不再有一次
    无意义的 in_progress -> pending 版本往返，因此 version 保持 0。
    """
    async def flow():
        async def boom(**kwargs):
            raise RuntimeError("spawn failed")
        host = TeamHost(scope(), quota_manager=FakeQuota(), spawn_fn=boom)
        await host.create("demo")
        task = await host.create_task(subject="x")
        with pytest.raises(RuntimeError):
            await host.spawn_teammate(task_name="a", message="do", team_task_id=task["task_id"])
        row = await host.get_task(task["task_id"])
        assert row["status"] == "pending" and row["owner_agent_id"] == ""
        assert row["version"] == task["version"], "失败的派活不应改动任务版本"

    run(flow())


def test_events_since_publishes_teammate_events(isolated_db):
    """teammate 写入的事件必须在 lead 拉取时进入 SSE sink，否则面板永远看不到。"""
    async def flow():
        lead_scope = scope(agent="root")
        mate_scope = scope(agent="sub:w", role="teammate")
        await team_store.emit_event(
            mate_scope, "task_updated",
            {"task": {"task_id": "t1", "status": "completed"}},
            task_id="t1",
        )
        seen: list[dict] = []
        lead = TeamHost(lead_scope, event_sink=seen.append)
        rows = await lead.events_since(0)
        assert len(rows) == 1
        assert len(seen) == 1
        assert seen[0].get("type") == "task_updated"

    run(flow())
