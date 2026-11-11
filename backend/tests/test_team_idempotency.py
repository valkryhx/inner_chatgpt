"""团队操作的幂等性：模型重做计划时，任务板不能长出僵尸任务。

真机 runId 125016-824c：lead 对「pending 且无 owner」的任务调用 reclaim，
平台返回无意义的 "conflict"，lead 据此以为释放失败，于是**重新创建**任务 2 和
任务 3 —— 任务板上留下 2 个永远没人认领的 pending 任务。

注意：这次运行赶上游 502 抖动，但**这两条不是上游导致的**：
reclaim 的返回值和 create_task 的去重都是纯本地逻辑，能脱离网络稳定复现。
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, team_store  # noqa: E402
from app.scope import TeamScope  # noqa: E402


def _scope(agent_id: str = "root", role: str = "lead") -> TeamScope:
    return TeamScope(user_id="u_" + uuid.uuid4().hex[:8],
                     session_id="s_" + uuid.uuid4().hex[:8],
                     generation_id="g1", team_id="team_" + uuid.uuid4().hex[:6],
                     agent_id=agent_id, role=role)


@pytest.mark.asyncio
async def test_reclaim_pending_unowned_task_is_idempotent():
    """核心回归：已经释放过的任务再 reclaim 一次，应返回成功而不是 conflict。"""
    await db.init_db()
    await team_store.init_tables()
    scope = _scope()

    task = await team_store.create_task(scope, subject="干活", description="d")
    assert task["status"] == "pending" and not task["owner_agent_id"]

    out = await team_store.reclaim_task(scope, task["task_id"],
                                        expected_version=task["version"])

    assert out["ok"] is True, f"已是 pending 无主，reclaim 应幂等成功，实际 {out}"
    assert out.get("already_released") is True

    await team_store.delete_session_data(scope.user_id, scope.session_id)


@pytest.mark.asyncio
async def test_reclaim_still_releases_owned_task():
    """正向对照：真占着任务时 reclaim 仍然把它释放回 pending。"""
    await db.init_db()
    await team_store.init_tables()
    scope = _scope()

    task = await team_store.create_task(scope, subject="干活", description="d")
    await team_store.bind_task_owner(scope, task["task_id"], "sub:worker",
                                     expected_version=task["version"])

    out = await team_store.reclaim_task(scope, task["task_id"],
                                        expected_version=task["version"] + 1)

    assert out["ok"] is True, out
    row = await team_store.get_task(scope, task["task_id"])
    assert row["status"] == "pending" and row["owner_agent_id"] == ""

    await team_store.delete_session_data(scope.user_id, scope.session_id)


@pytest.mark.asyncio
async def test_reclaim_unknown_task_reports_not_found():
    """不存在的任务仍要如实报 not_found，不能幂等地"成功"。"""
    await db.init_db()
    await team_store.init_tables()
    scope = _scope()

    out = await team_store.reclaim_task(scope, "task_does_not_exist",
                                        expected_version=0)

    assert out["ok"] is False and out["reason"] == "not_found", out


@pytest.mark.asyncio
async def test_create_task_dedupes_by_subject():
    """核心回归：同一团队同名且未完成的任务只保留一行。"""
    await db.init_db()
    await team_store.init_tables()
    scope = _scope()

    first = await team_store.create_task(scope, subject="任务2：算平均行数",
                                        description="d")
    second = await team_store.create_task(scope, subject="任务2：算平均行数",
                                         description="d")

    assert second["task_id"] == first["task_id"], \
        f"同名未完成任务应复用，实际新建了 {second['task_id']}"
    tasks = await team_store.list_tasks(scope)
    assert len(tasks) == 1, f"任务板不该长僵尸行，实际 {len(tasks)} 行"

    await team_store.delete_session_data(scope.user_id, scope.session_id)


@pytest.mark.asyncio
async def test_create_task_allows_recreate_after_complete():
    """正向对照：任务完成后重新创建同名任务应成功（那是新一轮工作）。"""
    await db.init_db()
    await team_store.init_tables()
    scope = _scope()

    first = await team_store.create_task(scope, subject="干活", description="d")
    bound = await team_store.bind_task_owner(
        scope, first["task_id"], "sub:worker", expected_version=first["version"])
    assert bound["ok"], bound
    # 完成任务必须由 owner 自己做（lead 代做会被权限挡掉，这是既有设计）
    worker = TeamScope(user_id=scope.user_id, session_id=scope.session_id,
                       generation_id=scope.generation_id, team_id=scope.team_id,
                       agent_id="sub:worker", role="teammate")
    done = await team_store.complete_task(
        worker, first["task_id"], expected_version=bound["task"]["version"])
    assert done["ok"], done

    second = await team_store.create_task(scope, subject="干活", description="d")

    assert second["task_id"] != first["task_id"], "完成之后应允许同名新建"

    await team_store.delete_session_data(scope.user_id, scope.session_id)


@pytest.mark.asyncio
async def test_create_task_dedup_is_team_scoped():
    """安全边界：不同团队的同名任务互不影响。"""
    await db.init_db()
    await team_store.init_tables()
    a, b = _scope(), _scope()

    ta = await team_store.create_task(a, subject="干活", description="d")
    tb = await team_store.create_task(b, subject="干活", description="d")

    assert ta["task_id"] != tb["task_id"], "跨团队不该互相去重"

    await team_store.delete_session_data(a.user_id, a.session_id)
    await team_store.delete_session_data(b.user_id, b.session_id)