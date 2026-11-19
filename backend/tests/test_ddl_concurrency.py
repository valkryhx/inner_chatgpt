"""多实例同时建表：`CREATE TABLE IF NOT EXISTS` 在 PG 上不是原子的。

## 事故（真跑 compose --scale app=2 时复现）

```
副本 A: BEGIN; CREATE TABLE IF NOT EXISTS users (...);  -- 检查不存在，去建
副本 B: BEGIN; CREATE TABLE IF NOT EXISTS users (...);  -- 同一时刻也检查不存在
       -> 两边都尝试插入 pg_type 系统表
       -> duplicate key value violates unique constraint "pg_type_typname_nsp_index"
       -> 副本 B: Application startup failed. Exiting.
```

PG 的 `IF NOT EXISTS` 是「先查后建」，两步之间没有锁。SQLite 不会这样：
它的 DDL 拿的是**库级写锁**，天然串行。

## 为什么不能只靠 leader

建表发生在 `lifespan` 很早的阶段（`db.init_db()`），那时 leader election
可能还没跑完；而且**非 leader 也必须能建表**（leader 可能后起）。
所以不能把建表变成 leader 专属。

## 修法

`executescript` 用一个**跨实例的 DDL 锁**包起来（PG advisory lock）。
锁的粒度是「整个建表脚本」，不是每条语句 —— DDL 只在启动跑一次，
串行化的代价可忽略。

**SQLite 路径完全不变**：`sqlite3` 的 DDL 已经自带库级写锁，
advisory lock 是 PG 专有的（`pg_advisory_xact_lock`）。

## 反向确认

把 `executescript` 里的 DDL 锁去掉，`test_concurrent_createscript_is_serialized`
必须变红（需要真 PG）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

PG_DSN = os.environ.get("HLZ_TEST_PG_DSN", "")
needs_pg = pytest.mark.skipif(
    not PG_DSN, reason="未设置 HLZ_TEST_PG_DSN，跳过 PG 集成测试")


# ---------------------------------------------------------------- 不需要真库

def test_ddl_lock_name_exists():
    """DDL 锁必须有个具名常量，便于文档与运维引用。"""
    from app import pgdriver
    assert hasattr(pgdriver, "DDL_LOCK_SCOPE"), "pgdriver 没有 DDL 锁的 scope 常量"
    assert pgdriver.DDL_LOCK_SCOPE, "DDL 锁 scope 不能为空"


def test_pg_executescript_references_ddl_lock():
    """PG 驱动的 `executescript` 必须引用 DDL 锁常量。

    注意看的是 **pgdriver.Connection.executescript**（PG 专用重写），
    不是 `sqlcompat.Connection.executescript`（SQLite 用的基类版本，
    那里不需要锁 —— sqlite3 的 DDL 自带库级写锁）。
    """
    import inspect

    from app import pgdriver

    src = inspect.getsource(pgdriver.PgConnection.executescript)
    assert "DDL_LOCK_SCOPE" in src, (
        "pgdriver.executescript 里没有引用 DDL 锁常量 —— "
        "多实例同时启动会撞 pg_type_typname_nsp_index")
    assert "wait_s" in src, "DDL 锁没有超时保护，卡死的持有者会拖死所有实例启动"


# ---------------------------------------------------------------- 真 PG

@needs_pg
def test_concurrent_createscript_is_serialized(tmp_path):
    """**核心**：两个连接**同时**跑同一段 CREATE TABLE，必须都成功。

    这就是 compose --scale 时发生的事：多个副本同时 `init_db()`。
    没有 DDL 锁时第二个会 `duplicate key value violates unique constraint
    "pg_type_typname_nsp_index"` 并让整个进程退出。
    """
    from app import dbselect, sqlcompat

    schema = dbselect.pg_schema_for(str(tmp_path / "concurrent.db"))

    async def go():
        async with sqlcompat.connect(tmp_path / "concurrent.db") as c1, \
                   sqlcompat.connect(tmp_path / "concurrent.db") as c2:
            # 先把 schema 建好，避免两边同时 CREATE SCHEMA
            await c1.executescript('CREATE TABLE IF NOT EXISTS warmup (a INTEGER)')
            ddl = """
            CREATE TABLE IF NOT EXISTS race_t (
                id INTEGER PRIMARY KEY,
                name TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_race_name ON race_t(name);
            CREATE TABLE IF NOT EXISTS race_u (
                id INTEGER PRIMARY KEY,
                ref INTEGER
            );
            """
            results = await asyncio.gather(
                c1.executescript(ddl),
                c2.executescript(ddl),
                return_exceptions=True,
            )
            return results

    results = asyncio.run(go())
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"并发建表失败：{[repr(e) for e in errors]}"


@needs_pg
def test_concurrent_alter_table_tolerated(tmp_path):
    """并发 `ALTER TABLE ADD COLUMN` 也不能让启动崩掉。

    与建表同一类问题：迁移代码是 `try: ALTER ... except: pass`，
    但 PG 里「列已存在」的错误会把整个事务打坏（除非有 SAVEPOINT）。
    """
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(tmp_path / "alt.db") as setup:
            await setup.executescript(
                "CREATE TABLE IF NOT EXISTS alt_t (id INTEGER PRIMARY KEY)")

        async def add_col():
            async with sqlcompat.connect(tmp_path / "alt.db") as c:
                try:
                    await c.execute("ALTER TABLE alt_t ADD COLUMN extra TEXT")
                    await c.commit()
                    return "ok"
                except Exception as exc:  # noqa: BLE001
                    return f"err:{exc.__class__.__name__}"

        return await asyncio.gather(add_col(), add_col())

    got = asyncio.run(go())
    assert any(g == "ok" for g in got), f"两边都失败了：{got}"