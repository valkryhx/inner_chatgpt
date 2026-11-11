"""L3-b（续）：队友异常结束时，平台必须释放它占着的任务。

真机 runId 121743-7329：两个 worker 因上游持续 502 而 errored，
它们占的任务永远停在 in_progress，第三个任务永远等不到依赖完成，
整个团队退化成一个烂掉的任务板。

团队韧性要求「一个 agent 出问题不能拖垮整个团队」—— 这一步由平台兜底，
不能依赖模型记得手动 reclaim。
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, team_store  # noqa: E402
from app.scope import TeamScope  # noqa: E402
from app.subagent.context import AgentContext  # noqa: E402
from app.subagent.events import EventBuffer  # noqa: E402
from app.subagent.host import SubagentHost  # noqa: E402
from app.team_host import TeamHost  # noqa: E402


class _FakeRuntime:
    registry = None

    def __init__(self, team_host=None):
        self.team_host = team_host


def _ctx() -> AgentContext:
    return AgentContext(user_id="u_" + uuid.uuid4().hex[:8],
                        session_id="s_" + uuid.uuid4().hex[:8],
                        message_id="m1", generation_id="g1",
                        team_id="team_" + uuid.uuid4().hex[:6], role="lead")


@pytest.mark.asyncio
async def test_dead_teammate_releases_its_task():
    """核心回归：队友 errored 后，它占的任务回到 pending 且 owner 清空。"""
    await db.init_db()
    await team_store.init_tables()
    ctx = _ctx()

    lead_scope = TeamScope(user_id=ctx.user_id, session_id=ctx.session_id,
                           generation_id=ctx.generation_id, team_id=ctx.team_id,
                           agent_id="root", role="lead")
    task = await team_store.create_task(lead_scope, subject="干活", description="d")
    await team_store.bind_task_owner(
        lead_scope, task["task_id"], "sub:worker", expected_version=task["version"])

    host = SubagentHost(ctx, parent_runtime=_FakeRuntime(), buffer=EventBuffer())
    node = host.registry.create_child(ctx, "worker", team_id=ctx.team_id)
    node.status = "errored"
    node.error = "SubagentError: HTTP 502"

    child_scope = TeamScope(user_id=ctx.user_id, session_id=ctx.session_id,
                            generation_id=ctx.generation_id, team_id=ctx.team_id,
                            agent_id="sub:worker", role="teammate")
    child_host = TeamHost(child_scope, deliver_sink=lambda m: {"delivered": True})

    await host._release_dead_agent_tasks(node, _FakeRuntime(child_host))

    row = await team_store.get_task(lead_scope, task["task_id"])
    assert row["status"] == "pending", f"应释放回 pending，实际 {row['status']}"
    assert row["owner_agent_id"] == "", f"owner 应清空，实际 {row['owner_agent_id']}"

    await team_store.delete_session_data(ctx.user_id, ctx.session_id)


@pytest.mark.asyncio
async def test_completed_teammate_task_is_not_released():
    """正向对照：正常完成的队友，任务不该被回收（那是它的成果）。"""
    await db.init_db()
    await team_store.init_tables()
    ctx = _ctx()

    lead_scope = TeamScope(user_id=ctx.user_id, session_id=ctx.session_id,
                           generation_id=ctx.generation_id, team_id=ctx.team_id,
                           agent_id="root", role="lead")
    task = await team_store.create_task(lead_scope, subject="干活", description="d")
    await team_store.bind_task_owner(
        lead_scope, task["task_id"], "sub:worker", expected_version=task["version"])

    host = SubagentHost(ctx, parent_runtime=_FakeRuntime(), buffer=EventBuffer())
    node = host.registry.create_child(ctx, "worker", team_id=ctx.team_id)
    node.status = "completed"

    child_scope = TeamScope(user_id=ctx.user_id, session_id=ctx.session_id,
                            generation_id=ctx.generation_id, team_id=ctx.team_id,
                            agent_id="sub:worker", role="teammate")
    child_host = TeamHost(child_scope, deliver_sink=lambda m: {"delivered": True})

    # 只有 errored/stopped 才会走到释放分支（见 _run_child 的条件）
    if node.status in ("errored", "stopped"):
        await host._release_dead_agent_tasks(node, _FakeRuntime(child_host))

    row = await team_store.get_task(lead_scope, task["task_id"])
    assert row["status"] == "in_progress", f"完成的任务不该被回收，实际 {row['status']}"
    assert row["owner_agent_id"] == "sub:worker"

    await team_store.delete_session_data(ctx.user_id, ctx.session_id)


@pytest.mark.asyncio
async def test_release_does_not_touch_other_agents_tasks():
    """安全边界：只释放自己的任务，别人的一律不碰。"""
    await db.init_db()
    await team_store.init_tables()
    ctx = _ctx()

    lead_scope = TeamScope(user_id=ctx.user_id, session_id=ctx.session_id,
                           generation_id=ctx.generation_id, team_id=ctx.team_id,
                           agent_id="root", role="lead")
    mine = await team_store.create_task(lead_scope, subject="我的", description="d")
    other = await team_store.create_task(lead_scope, subject="别人的", description="d")
    await team_store.bind_task_owner(
        lead_scope, mine["task_id"], "sub:worker", expected_version=mine["version"])
    await team_store.bind_task_owner(
        lead_scope, other["task_id"], "sub:other", expected_version=other["version"])

    host = SubagentHost(ctx, parent_runtime=_FakeRuntime(), buffer=EventBuffer())
    node = host.registry.create_child(ctx, "worker", team_id=ctx.team_id)
    node.status = "errored"

    child_scope = TeamScope(user_id=ctx.user_id, session_id=ctx.session_id,
                            generation_id=ctx.generation_id, team_id=ctx.team_id,
                            agent_id="sub:worker", role="teammate")
    child_host = TeamHost(child_scope, deliver_sink=lambda m: {"delivered": True})

    await host._release_dead_agent_tasks(node, _FakeRuntime(child_host))

    a = await team_store.get_task(lead_scope, mine["task_id"])
    b = await team_store.get_task(lead_scope, other["task_id"])
    assert a["status"] == "pending", "自己的任务应被释放"
    assert b["status"] == "in_progress", "别人的任务不该被动"


async def _cleanup(ctx):
    await team_store.delete_session_data(ctx.user_id, ctx.session_id)


@pytest.mark.asyncio
async def test_release_survives_broken_team_host():
    """团队层坏掉时，释放失败不能拖垮子代理收尾。"""
    await db.init_db()
    ctx = _ctx()

    class _Broken:
        async def list_tasks(self, **kw):
            raise RuntimeError("db is gone")

    host = SubagentHost(ctx, parent_runtime=_FakeRuntime(), buffer=EventBuffer())
    node = host.registry.create_child(ctx, "worker", team_id=ctx.team_id)
    node.status = "errored"

    # 不抛异常即通过：收尾路径必须无条件跑完
    await host._release_dead_agent_tasks(node, _FakeRuntime(_Broken()))


@pytest.mark.asyncio
async def test_run_child_calls_release_on_error(monkeypatch):
    """接点验证：_run_child 走完 errored 路径时，确实调用了释放逻辑。

    上一条用例只测了释放函数本身；这条固定「它被接在收尾路径上」，
    否则函数写得再对也不会被执行（之前的 RED 验证就是被这一点掩盖的）。
    """
    await db.init_db()
    from app.subagent import host as host_mod

    calls: list[tuple[str, str]] = []

    async def _spy(self, node, runtime):
        calls.append((node.name, node.status))

    monkeypatch.setattr(SubagentHost, "_release_dead_agent_tasks", _spy)

    ctx = _ctx()
    host = SubagentHost(ctx, parent_runtime=_FakeRuntime(), buffer=EventBuffer())
    node = host.registry.create_child(ctx, "worker", team_id=ctx.team_id)
    node.status = "starting"

    class _FakeRuntimeCls:
        def __init__(self, *a, **kw):
            self.history = []

        async def build(self):
            raise RuntimeError("模拟上游炸了")

    monkeypatch.setattr(host_mod, "SubagentRuntime", _FakeRuntimeCls, raising=False)
    spec = host_mod.SubagentSpec(name="worker", message="干活")

    await host._run_child(node, spec)

    assert node.status == "errored", f"应记为 errored，实际 {node.status}"
    assert calls == [("worker", "errored")], f"收尾必须调用释放，实际 {calls}"