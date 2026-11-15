"""Regression tests for the 2026-11-06 web self-test bug list.

Each test maps to one confirmed bug:

- BUG-01: a child that inherits the parent's team must be a teammate.
- BUG-02: the Team lifecycle lease must be released synchronously, even when
  the turn cleanup is interrupted by ``CancelledError``.
- BUG-06: soft-line compaction must end under the soft line.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app import team_store
from app.scope import TeamScope
from app.subagent.context import AgentContext
from app.subagent.registry import SubagentRegistry
from app.team_host import TeamHost


def run(coro):
    return asyncio.run(coro)


# ---------------- BUG-01 ----------------
def _ctx(role="lead", team_id="t1") -> AgentContext:
    return AgentContext(
        user_id="u1", session_id="s1", message_id="m1", generation_id="g1",
        team_id=team_id, role=role,
    )


def test_child_inheriting_parent_team_is_teammate():
    reg = SubagentRegistry(max_depth=3, max_active=4)
    node = reg.create_child(_ctx(), "scout")
    assert node.ctx.team_id == "t1"
    assert node.role == "teammate"
    assert node.ctx.role == "teammate"


def test_child_with_explicit_team_is_teammate():
    reg = SubagentRegistry(max_depth=3, max_active=4)
    node = reg.create_child(_ctx(team_id=""), "scout", team_id="t9")
    assert node.ctx.team_id == "t9"
    assert node.role == "teammate"


def test_child_outside_team_keeps_requested_role():
    reg = SubagentRegistry(max_depth=3, max_active=4)
    node = reg.create_child(_ctx(role="root", team_id=""), "scout", role="general")
    assert node.role == "general"


# ---------------- BUG-02 ----------------
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


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(team_store, "DB_PATH", tmp_path / "team.db")
    run(team_store.init_tables())


def test_release_lifecycle_now_is_sync_and_idempotent(isolated_db):
    async def flow():
        quota = FakeQuota()
        host = TeamHost(
            TeamScope("u1", "s1", "g1", "t1", "root", "lead"),
            quota_manager=quota,
        )
        await host.create("demo")
        assert quota.released == 0
        host.release_lifecycle_now()
        assert quota.released == 1
        host.release_lifecycle_now()
        assert quota.released == 1

    run(flow())


def test_cleanup_turn_releases_lease_before_interrupted_awaits(monkeypatch):
    from app.routers import chat

    released = {"now": 0, "status": []}

    class Team:
        observed_seq = 0

        def __init__(self):
            self.lease_released = False

        def release_lifecycle_now(self):
            # Must be callable from a cancelled task: no await inside.
            self.lease_released = True
            released["now"] += 1

        async def events_since(self, seq, limit=200):
            return []

        async def set_status(self, status):
            released["status"].append(status)

    class Host:
        def __init__(self):
            self.stop_called = False

        def stop_all_now(self):
            self.stop_called = True

        async def close_all(self):
            # anyio re-raises cancellation at a cancelled await.
            raise asyncio.CancelledError()

    class Hub:
        def unregister_subagent_host(self, *args):
            released["unregister"] = args

    team = Team()
    host = Host()
    turn = type("Turn", (), {"team": team, "host": host})()
    hub = Hub()

    async def flow():
        await asyncio.wait_for(
            chat._cleanup_turn(turn, hub, "u1", "s1", "g1", "t1", "completed"),
            timeout=2,
        )
        await asyncio.sleep(0)  # let the fallback settle task run
        await asyncio.sleep(0.05)

    run(flow())
    assert team.lease_released is True
    assert host.stop_called is True
    assert released["status"] == ["completed"]


# ---------------- BUG-06 ----------------
class FakeStore:
    def __init__(self, history):
        self.history = list(history)

    async def get(self, user_id, session_id):
        return list(self.history)

    async def set(self, user_id, session_id, history):
        self.history = list(history)


class FakeClient:
    def __init__(self, text):
        self.text = text
        self.calls: list[int | None] = []

    async def run(self, messages, max_tokens=None):
        self.calls.append(max_tokens)
        return type("Result", (), {"content": self.text})()


class FakeUR:
    soft_limit = 1200
    hard_limit = 1800

    def __init__(self, history, summary_text):
        self.store = FakeStore(history)
        self.model = type("Model", (), {"client": FakeClient(summary_text)})()


def _history(n=10, chars=200):
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": "字" * chars})
    return out


def test_soft_compaction_ends_under_soft_line(monkeypatch):
    import app.compaction as compaction

    ur = FakeUR(_history(), "摘" * 700)
    monkeypatch.setattr(compaction, "get_hub", lambda: type(
        "Hub", (), {"get_user_runtime": staticmethod(lambda *a, **k: _async(ur))}
    )())

    async def fake_get_session(session_id, user_id):
        return {"id": session_id, "model_id": "m"}

    monkeypatch.setattr(compaction.db, "get_session", fake_get_session)
    monkeypatch.setattr(compaction, "db", compaction.db)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(compaction, "_record_visible_summary", noop)

    stats = run(compaction.enforce_soft_limit("u1", "s1"))
    assert stats is not None
    assert stats["before_tokens"] > stats["soft_limit"]
    assert stats["after_tokens"] <= stats["soft_limit"]
    # A summary larger than the recovery target must trigger a second pass
    # (fold part of the preserved tail) or a final tail trim, never an
    # "already compacted but still over the line" result.
    assert len(ur.model.client.calls) >= 1
    assert all(cap is None or cap <= 4096 for cap in ur.model.client.calls)


def test_manual_compact_reports_same_kept_count(monkeypatch):
    import app.compaction as compaction

    ur = FakeUR(_history(n=10), "摘" * 120)
    monkeypatch.setattr(compaction, "get_hub", lambda: type(
        "Hub", (), {"get_user_runtime": staticmethod(lambda *a, **k: _async(ur))}
    )())

    async def fake_get_session(session_id, user_id):
        return {"id": session_id, "model_id": "m"}

    monkeypatch.setattr(compaction.db, "get_session", fake_get_session)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(compaction, "_record_visible_summary", noop)

    stats = run(compaction.manual_compact("u1", "s1", keep=3))
    assert stats["compacted"] is True
    # The notice and the summary card must use the same "kept" definition.
    assert f"保留 {stats['kept_messages']} 条" in stats["message"]
    assert stats["after_messages"] == stats["kept_messages"]


def _async(value):
    async def _coro():
        return value
    return _coro()


def test_manual_compact_too_few_reports_tokens_not_just_count(monkeypatch):
    """「消息太少」必须带 token 数。

    `keep` 是**条数**阈值，与 token 无关。只说「6 条」时，用户看到 4664 tokens
    也在里面会觉得「这么多字应该能压」——2026-11-15 挑剔用户视角验收时
    这条被记为「反直觉」。把 token 一并说出来，判据才有个解释。

    判据：消息数 ≤ keep 时 `compacted=False`，且 message 里同时出现条数与 token。
    """
    import app.compaction as compaction

    n = 4
    ur = FakeUR(_history(n=n), "摘" * 20)
    monkeypatch.setattr(compaction, "get_hub", lambda: type(
        "Hub", (), {"get_user_runtime": staticmethod(lambda *a, **k: _async(ur))}
    )())

    async def fake_get_session(session_id, user_id):
        return {"id": session_id, "model_id": "m"}

    monkeypatch.setattr(compaction.db, "get_session", fake_get_session)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(compaction, "_record_visible_summary", noop)

    stats = run(compaction.manual_compact("u1", "s1", keep=6))
    assert stats["compacted"] is False
    msg = stats["message"]
    assert "消息太少" in msg
    assert f"{n} 条" in msg, "必须保留条数"
    assert "tokens" in msg, "必须同时给出 token 数，否则「这么多字为什么不能压」无解"
    # token 数必须是真实估算值，不能写死
    from app.tokens import message_token_counter
    assert str(message_token_counter(_history(n=n))) in msg
