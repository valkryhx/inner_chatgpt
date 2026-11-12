import asyncio
import pytest

from app.quota import Limits, QuotaExceeded, QuotaManager
from app.scope import TeamScope


def run(coro):
    return asyncio.run(coro)


def test_code_run_user_limit_and_idempotent_release():
    async def flow():
        q = QuotaManager(Limits(global_code_runs=4, user_code_runs=1))
        async with q.code_run("u1", timeout_s=0.1):
            with pytest.raises(QuotaExceeded):
                async with q.code_run("u1", timeout_s=0.02):
                    pass
            async with q.code_run("u2", timeout_s=0.1):
                pass
    run(flow())


def test_user_subagents_are_shared_across_sessions():
    async def flow():
        q = QuotaManager(Limits(global_subagents=8, user_subagents=1, team_subagents=4))
        a = TeamScope("u1", "s1", "g1", "t1")
        b = TeamScope("u1", "s2", "g2", "t2")
        async with q.subagent(a, timeout_s=0.1):
            with pytest.raises(QuotaExceeded):
                async with q.subagent(b, timeout_s=0.02):
                    pass
            async with q.subagent(TeamScope("u2", "s9", "g9", "t9"), timeout_s=0.1):
                pass
    run(flow())


def test_team_limit():
    async def flow():
        q = QuotaManager(Limits(global_teams=4, user_teams=2, session_teams=1))
        s = TeamScope("u1", "s1", "g1", "t1")
        async with q.team(s, timeout_s=0.1):
            with pytest.raises(QuotaExceeded):
                async with q.team(TeamScope("u1", "s1", "g1", "t2"), timeout_s=0.02):
                    pass
            async with q.team(TeamScope("u1", "s2", "g2", "t3"), timeout_s=0.1):
                pass
    run(flow())


def test_default_user_team_limit_allows_two_sessions():
    """默认配置下同一用户的第二个会话必须能开 Team。

    历史默认 USER_MAX_ACTIVE_TEAMS=1，会话级配额也是 1，于是用户开第二个标签页
    问一句「你好」就拿到 Team 本轮不可用：QuotaExceeded: capacity_exceeded after 2.0s。
    会话级闸门已经保证一会话一 Team，用户级只需要管跨用户公平性。
    """
    async def flow():
        q = QuotaManager(Limits())
        assert q.limits.user_teams >= 2, "用户级 Team 上限不应把单用户多会话锁死"
        async with q.team(TeamScope("u1", "s1", "g1", "t1"), timeout_s=0.1):
            async with q.team(TeamScope("u1", "s2", "g2", "t2"), timeout_s=0.1):
                pass
    run(flow())


def test_team_quota_error_names_the_full_gate():
    """配额报错必须能区分是哪一级满了。"""
    async def flow():
        q = QuotaManager(Limits(global_teams=4, user_teams=1, session_teams=1))
        async with q.team(TeamScope("u1", "s1", "g1", "t1"), timeout_s=0.1):
            with pytest.raises(QuotaExceeded) as ei:
                await q.team_lease(TeamScope("u1", "s2", "g2", "t2"), timeout_s=0.05)
        msg = str(ei.value)
        assert "capacity_exceeded" in msg
        assert "用户 Team 0/1" in msg, msg
        assert "session=s2" in msg, msg
    run(flow())


def test_mcp_call_user_limit_and_release(monkeypatch):
    import app.mcp_client as mc
    import app.quota as quota

    class FakeClient:
        async def call_tool(self, name, args):
            return {"ok": True}

    async def flow():
        q = QuotaManager(Limits(global_mcp_calls=4, user_mcp_calls=1))
        monkeypatch.setattr(quota, "get_quota", lambda: q)
        toolset = mc.MCPToolset(user_id="u1")
        toolset._client = FakeClient()
        tool = toolset._wrap(type("T", (), {
            "name": "probe", "description": "probe", "inputSchema": {"type": "object"},
        })())

        first = asyncio.create_task(tool.call({}))
        await asyncio.sleep(0)
        # The fake call is immediate; use the semaphore directly to assert it was
        # actually released and can be reacquired by the same user.
        assert "ok" in await asyncio.wait_for(first, timeout=1)
        async with q.mcp_call("u1", timeout_s=0.1):
            pass

    run(flow())
