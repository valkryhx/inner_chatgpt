"""多实例下的 stale 恢复：**不能清掉别的实例正在跑的 generation**。

## 事故场景

```
实例 A 正在给用户跑 generation g1（session_generations 里 status='active'）
实例 B 重启 -> lifespan 里调 recover_stale_runtime("server restart")
           -> 无差别 UPDATE ... WHERE status='active'
           -> A 的 g1 被标成 stale
           -> A 那边还在写，B 这边认为它死了：状态分叉
```

旧实现的前提是「单进程，重启时不可能有别的进程在跑」，多实例下不成立。

## 修法

`session_generations` 加 `owner` 列记 `<host>:<pid>`，恢复时只清
**owner 不在存活实例集合里**的行。没配 Redis 时存活集合为空
=> 全清（正是单实例语义，行为不变）。

## 反向确认

把 `recover_stale_runtime` 的 owner 过滤去掉（回到无差别 UPDATE），
`test_live_instance_generation_survives` 必须变红。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """独立的 SQLite 库（全部 team 相关模块都要打补丁）。"""
    from app import team_store

    db = tmp_path / "app.db"
    monkeypatch.setattr(team_store, "DB_PATH", db)
    run(team_store.init_tables())
    return db


def _acquire(user_id: str, session_id: str, gen: str):
    from app import team_store
    return run(team_store.acquire_generation(user_id, session_id, gen))


def test_generation_records_owner(isolated_db):
    """新建 generation 必须记下自己的 owner（否则恢复时无法判活）。"""
    from app import team_store, sqlcompat
    from app.sandbox.identity import owner_id

    assert _acquire("u1", "s1", "g1") is True

    async def go():
        async with sqlcompat.connect(team_store.DB_PATH) as conn:
            conn.row_factory = sqlcompat.Row
            cur = await conn.execute(
                "SELECT owner FROM session_generations WHERE generation_id='g1'")
            row = await cur.fetchone()
            return dict(row) if row else None

    row = run(go())
    assert row is not None
    assert row["owner"] == owner_id(), f"owner 没记对：{row['owner']}"


def test_live_instance_generation_survives(isolated_db):
    """**核心**：正在跑的实例的 generation 不能被别的实例清掉。"""
    from app import team_store

    # 用别的「实例」的 owner 造一条 active（模拟实例 A）
    live_owner = "hostA:111"
    _seed_generation(isolated_db, "u1", "s1", "g_a", status="active",
                     owner=live_owner)

    stats = run(team_store.recover_stale_runtime(
        "test", live_owners={live_owner}))

    assert stats["generations"] == 0, "清掉了存活实例的 generation"
    assert _status(isolated_db, "g_a") == "active"


def test_dead_instance_generation_is_recovered(isolated_db):
    """死掉的实例（owner 不在存活集合）留下的 generation 必须被清。"""
    from app import team_store

    _seed_generation(isolated_db, "u1", "s1", "g_dead", status="active",
                     owner="hostA:999")

    stats = run(team_store.recover_stale_runtime(
        "test", live_owners={"hostA:111"}))

    assert stats["generations"] == 1
    assert _status(isolated_db, "g_dead") == "stale"


def test_legacy_rows_without_owner_are_recovered(isolated_db):
    """老库没有 owner 列（空串）—— 视为死实例，必须清。"""
    from app import team_store

    _seed_generation(isolated_db, "u1", "s1", "g_legacy", status="active",
                     owner="")

    stats = run(team_store.recover_stale_runtime(
        "test", live_owners={"hostA:111"}))

    assert stats["generations"] == 1
    assert _status(isolated_db, "g_legacy") == "stale"


def test_single_instance_still_clears_everything(isolated_db):
    """没配 Redis（存活集合为空）时全清 —— 单实例语义不能变。"""
    from app import team_store

    _seed_generation(isolated_db, "u1", "s1", "g1", status="active", owner="h:1")
    _seed_generation(isolated_db, "u1", "s2", "g2", status="active", owner="h:2")

    stats = run(team_store.recover_stale_runtime("test"))   # live_owners 不传

    assert stats["generations"] == 2
    assert _status(isolated_db, "g1") == "stale"
    assert _status(isolated_db, "g2") == "stale"


def test_tasks_of_live_instance_protected(isolated_db):
    """in_progress 的 team task 同理：活着的实例的不能被打回 pending。"""
    from app import team_store

    live = "hostA:111"
    _seed_task(isolated_db, team_id="t_live", task_id="task1",
               status="in_progress", owner=live)
    _seed_task(isolated_db, team_id="t_dead", task_id="task2",
               status="in_progress", owner="hostA:999")

    stats = run(team_store.recover_stale_runtime("test", live_owners={live}))

    assert stats["tasks"] == 1, "应该只恢复死实例的任务"
    assert _task_status(isolated_db, "t_live", "task1") == "in_progress"
    assert _task_status(isolated_db, "t_dead", "task2") == "pending"


# ---------------------------------------------------------------- 辅助

def _seed_generation(db, user_id, session_id, gen, *, status, owner):
    from app import team_store, sqlcompat

    async def go():
        async with sqlcompat.connect(db) as conn:
            await conn.execute(
                "INSERT INTO session_generations"
                "(user_id,session_id,generation_id,status,started_at,owner) "
                "VALUES(?,?,?,?,?,?)",
                (user_id, session_id, gen, status, 1, owner))
            await conn.commit()

    run(go())


def _seed_task(db, *, team_id, task_id, status, owner):
    from app import team_store, sqlcompat
    import json

    async def go():
        async with sqlcompat.connect(db) as conn:
            await conn.execute(
                "INSERT INTO team_tasks(team_id,user_id,session_id,generation_id,"
                "task_id,subject,status,owner_agent_id,owner,created_by,"
                "version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (team_id, "u1", "s1", "g1", task_id, "x", status, "",
                 owner, "root", 1, 1, 1))
            await conn.commit()

    run(go())


def _status(db, gen) -> str:
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(db) as conn:
            cur = await conn.execute(
                "SELECT status FROM session_generations WHERE generation_id=?", (gen,))
            row = await cur.fetchone()
            return row[0] if row else None

    return run(go())


def _task_status(db, team_id, task_id) -> str:
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(db) as conn:
            cur = await conn.execute(
                "SELECT status FROM team_tasks WHERE team_id=? AND task_id=?",
                (team_id, task_id))
            row = await cur.fetchone()
            return row[0] if row else None

    return run(go())