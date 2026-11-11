"""L3-b 韧性：一个队友出问题，任务能被重新接走（团队不退化）。

场景：worker 跑任务时 LLM 彻底挂了 → 它 errored 结束 → 任务还挂在 in_progress。
如果只能靠模型记得手动 reclaim，模型一旦没想到，任务板就烂在那里。
平台应该在原 owner 确实已死时允许直接重绑。
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
from app.subagent import store as sub_store  # noqa: E402


def _scope(agent_id: str = "root", role: str = "lead") -> TeamScope:
    return TeamScope(user_id="u_" + uuid.uuid4().hex[:8],
                     session_id="s_" + uuid.uuid4().hex[:8],
                     generation_id="g1", team_id="team_" + uuid.uuid4().hex[:6],
                     agent_id=agent_id, role=role)


async def _mk_task(scope: TeamScope, subject: str = "干活") -> dict:
    return await team_store.create_task(scope, subject=subject, description="d")


async def _finish_agent(scope: TeamScope, agent_id: str, status: str) -> None:
    run_id = "run_" + uuid.uuid4().hex[:10]
    await sub_store.create_run(
        run_id=run_id, ctx_user_id=scope.user_id, session_id=scope.session_id,
        message_id="m1", parent_agent_id="root", agent_id=agent_id, name=agent_id,
        role="teammate", task_message="t", status="running",
        generation_id=scope.generation_id, team_id=scope.team_id)
    await sub_store.finish_run(
        run_id, status=status, result_text="", turns=1, error="boom",
        user_id=scope.user_id, session_id=scope.session_id,
        generation_id=scope.generation_id, team_id=scope.team_id)


@pytest.mark.asyncio
async def test_task_of_dead_owner_can_be_taken_over():
    """核心回归：owner errored 后，另一个 agent 能直接接管它的任务。"""
    await db.init_db()
    await team_store.init_tables()
    scope = _scope()

    task = await _mk_task(scope)
    bound = await team_store.bind_task_owner(
        scope, task["task_id"], "sub:worker_a", expected_version=task["version"])
    assert bound["ok"], bound

    # worker_a 跑挂了
    await _finish_agent(scope, "sub:worker_a", "errored")

    again = await team_store.bind_task_owner(
        scope, task["task_id"], "sub:worker_b", expected_version=1)

    assert again["ok"] is True, f"死掉的 owner 的任务应可被接管，实际 {again}"
    assert again["task"]["owner_agent_id"] == "sub:worker_b"

    await team_store.delete_session_data(scope.user_id, scope.session_id)


@pytest.mark.asyncio
async def test_task_of_live_owner_is_not_stolen():
    """反向确认：owner 还活着时，任务不能被抢。"""
    await db.init_db()
    await team_store.init_tables()
    scope = _scope()

    task = await _mk_task(scope)
    await team_store.bind_task_owner(
        scope, task["task_id"], "sub:worker_a", expected_version=task["version"])
    # worker_a 还在跑（没有 finish_run 记录 = 活着）

    again = await team_store.bind_task_owner(
        scope, task["task_id"], "sub:worker_b", expected_version=1)

    assert again["ok"] is False, f"活着的 owner 的任务不能被抢，实际 {again}"

    await team_store.delete_session_data(scope.user_id, scope.session_id)


@pytest.mark.asyncio
async def test_task_of_completed_owner_is_not_stolen():
    """反向确认：owner 正常完成（但没把任务置 completed）时也不该被抢。"""
    await db.init_db()
    await team_store.init_tables()
    scope = _scope()

    task = await _mk_task(scope)
    await team_store.bind_task_owner(
        scope, task["task_id"], "sub:worker_a", expected_version=task["version"])
    await _finish_agent(scope, "sub:worker_a", "completed")

    again = await team_store.bind_task_owner(
        scope, task["task_id"], "sub:worker_b", expected_version=1)

    assert again["ok"] is False, f"completed 不算死，不该被抢，实际 {again}"

    await team_store.delete_session_data(scope.user_id, scope.session_id)


@pytest.mark.asyncio
async def test_takeover_still_respects_blocked_by():
    """接管也要守依赖：被阻塞的任务谁都不能绑。"""
    await db.init_db()
    await team_store.init_tables()
    scope = _scope()

    first = await _mk_task(scope, "第一步")
    second = await team_store.create_task(
        scope, subject="第二步", description="d", blocked_by=[first["task_id"]])
    await team_store.bind_task_owner(
        scope, second["task_id"], "sub:worker_a", expected_version=second["version"])
    await _finish_agent(scope, "sub:worker_a", "errored")

    again = await team_store.bind_task_owner(
        scope, second["task_id"], "sub:worker_b", expected_version=1)

    assert again["ok"] is False and again["reason"] == "blocked", \
        f"被阻塞的任务不能被接管，实际 {again}"

    await team_store.delete_session_data(scope.user_id, scope.session_id)