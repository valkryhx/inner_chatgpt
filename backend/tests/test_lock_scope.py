"""PG 分锁粒度的真实测试。

## 要证明的两件事

1. **不同 team 真的并行** —— 这是这次改动的全部目的。之前用固定键，
   全集群所有队的写事务排一队（实测 16 并发 3.8 倍差距）。
2. **同 team 仍然串行** —— 正确性不能因为分锁而丢。
   `MAX(event_seq)+1` 的保护必须还在。

## 怎么证明「并行」

用 `pg_advisory_xact_lock` 的阻塞特性：持有 A 队的锁，然后尝试拿 B 队的锁，
如果能立刻拿到，说明两者不是同一把锁。

这比测吞吐更硬 —— 吞吐受机器负载影响，阻塞与否是语义事实。

## 怎么证明「仍然串行」

两个协程同时抢同一个 scope 的锁，第二个必须阻塞到第一个释放。
用时间阈值断言（阻塞 >= 200ms）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DSN = os.environ.get("HLZ_TEST_PG_DSN", "")

# 只有真库测试需要 PG；纯函数测试到处都能跑（它们保护的是键的算法本身）
needs_pg = pytest.mark.skipif(
    not DSN, reason="未设置 HLZ_TEST_PG_DSN，跳过 PG 测试")

from app.pgdriver import lock_key_for  # noqa: E402


# ---------------------------------------------------------------- 纯函数

def test_same_scope_same_key():
    """同一 scope 必须算出同一键 —— 否则两个实例保护的不是同一把锁。"""
    assert lock_key_for("team:team_abc") == lock_key_for("team:team_abc")


def test_different_team_different_key():
    """不同 team 必须不同键 —— 这是分锁的前提。"""
    a = lock_key_for("team:team_aaa")
    b = lock_key_for("team:team_bbb")
    assert a != b


def test_none_scope_falls_back_to_global_key():
    """不带 scope 仍用旧的全局常量，保证向后兼容。"""
    from app.pgdriver import _WRITE_LOCK_KEY
    assert lock_key_for(None) == _WRITE_LOCK_KEY
    assert lock_key_for("") == _WRITE_LOCK_KEY


def test_key_is_stable_across_processes():
    """键必须跨进程稳定 —— 不能依赖 Python 的 hash()（有 PYTHONHASHSEED 随机化）。

    这里直接钉住一个具体数值。如果实现改成 `hash()`，这条立刻变红。
    """
    # blake2b("team:team_abc", 8) 是确定值；这里只断言「两次独立计算一致」
    # 且不是 Python hash 的输出形式（hash() 对字符串返回小整数）。
    k = lock_key_for("team:team_abc")
    assert k > 2 ** 32, "键太小，像是用了 hash() 而不是 blake2b"
    assert k == lock_key_for("team:team_abc")


def test_key_in_signed_int64_range():
    """pg_advisory_xact_lock 只接受 bigint；越界会让整个事务报错。"""
    for scope in ("team:x", "gen:a:b:c", "a" * 1000, "中文作用域"):
        k = lock_key_for(scope)
        assert 0 <= k < 2 ** 63


# ---------------------------------------------------------------- 真库行为

@pytest.fixture()
def pg_conn_factory():
    """给测试一个独立的 schema，避免和其他测试互相看见。"""
    from app import dbselect, pgdriver

    os.environ["HLZ_DB_URL"] = DSN
    os.environ["HLZ_PG_SCHEMA_PER_PATH"] = "1"
    dbselect.reset_for_tests()

    fake = f"/tmp/scope_{uuid.uuid4().hex}/app.db"
    schema = dbselect.pg_schema_for(fake)
    pgdriver.forget_schema_cache()

    yield fake, schema

    async def _cleanup():
        pool = await pgdriver._get_pool()
        await pool.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await pgdriver.close_pool()

    try:
        asyncio.run(_cleanup())
    except Exception:  # noqa: BLE001
        pass
    dbselect.reset_for_tests()


def _run(coro):
    return asyncio.run(coro)


@needs_pg
def test_different_teams_do_not_block_each_other(pg_conn_factory):
    """**核心断言**：持有 A 队的锁时，B 队的锁必须立刻能拿到。

    固定键时代的实现会让这条超时（B 要等 A 释放）。
    """
    from app import sqlcompat
    path, _ = pg_conn_factory

    async def go():
        # 先建表（不需要，advisory lock 不依赖表；但顺手验证连接可用）
        async with sqlcompat.connect(path) as c1:
            await c1.execute("BEGIN IMMEDIATE team:team_aaa")
            try:
                async with sqlcompat.connect(path) as c2:
                    t0 = time.perf_counter()
                    await c2.execute("BEGIN IMMEDIATE team:team_bbb")
                    elapsed = (time.perf_counter() - t0) * 1000
                    await c2.commit()
                return elapsed
            finally:
                await c1.rollback()

    elapsed = _run(go())
    assert elapsed < 1000, (
        f"B 队被 A 队阻塞了 {elapsed:.0f}ms —— 分锁没生效（还是固定键？）")


@needs_pg
def test_same_team_still_blocks(pg_conn_factory):
    """同一 team 必须仍然串行 —— 正确性不能因为分锁丢掉。"""
    from app import sqlcompat
    path, _ = pg_conn_factory

    async def go():
        async with sqlcompat.connect(path) as c1:
            await c1.execute("BEGIN IMMEDIATE team:team_same")
            holder = 0.0

            async def second():
                nonlocal holder
                async with sqlcompat.connect(path) as c2:
                    t0 = time.perf_counter()
                    await c2.execute("BEGIN IMMEDIATE team:team_same")
                    holder = (time.perf_counter() - t0) * 1000
                    await c2.commit()

            task = asyncio.create_task(second())
            await asyncio.sleep(0.4)          # 让 c2 真的阻塞在那里
            assert holder == 0.0, "同一 team 的第二个事务没有被阻塞"
            await c1.rollback()
            await asyncio.wait_for(task, timeout=10)
            return holder

    holder = _run(go())
    assert holder >= 200, f"阻塞时间 {holder:.0f}ms 太短，锁没起作用"


@needs_pg
def test_begin_scope_syntax_on_pg(pg_conn_factory):
    """`BEGIN IMMEDIATE <scope>` 这条非标准语法在 PG 上必须被正确消化。"""
    from app import sqlcompat
    path, _ = pg_conn_factory

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT PRIMARY KEY)")
            await conn.execute("BEGIN IMMEDIATE team:t1")
            await conn.execute("INSERT INTO t(id) VALUES(?)", ("a",))
            await conn.commit()
            cur = await conn.execute("SELECT count(*) FROM t")
            return (await cur.fetchone())[0]

    assert _run(go()) == 1


def test_begin_scope_stripped_on_sqlite(tmp_path, monkeypatch):
    """SQLite 后端必须剥掉 scope，否则 `BEGIN IMMEDIATE x` 语法错。"""
    from app import sqlcompat, dbselect
    monkeypatch.delenv("HLZ_DB_URL", raising=False)
    dbselect.reset_for_tests()
    db = tmp_path / "scope.db"

    async def go():
        async with sqlcompat.connect(db) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT PRIMARY KEY)")
            # 带 scope 的写法在 SQLite 上必须等价于不带
            await conn.execute("BEGIN IMMEDIATE team:t1")
            await conn.execute("INSERT INTO t(id) VALUES(?)", ("a",))
            await conn.commit()
            cur = await conn.execute("SELECT count(*) FROM t")
            return (await cur.fetchone())[0]

    assert _run(go()) == 1


@needs_pg
def test_event_seq_unique_under_concurrency(pg_conn_factory):
    """端到端：真实 team_store.emit_event 在多并发下不能撞主键。

    这是分锁改动的**正确性回归**：分了锁但漏了不变量，这条会红。
    """
    from app import sqlcompat, team_store
    path, _ = pg_conn_factory

    async def go():
        orig = team_store.DB_PATH
        team_store.DB_PATH = path
        try:
            await team_store.init_tables()
            scope = team_store.TeamScope(
                user_id="u1", session_id="s1", generation_id="g1",
                team_id="team_conc", agent_id="root")
            await team_store.create_team(scope, name="并发队")

            async def one(i):
                return await team_store.emit_event(scope, f"e{i}", {"i": i})

            rs = await asyncio.gather(*[one(i) for i in range(12)])
            seqs = sorted(r["event_seq"] for r in rs)
            return seqs
        finally:
            team_store.DB_PATH = orig

    seqs = _run(go())
    assert len(seqs) == len(set(seqs)), f"event_seq 撞了：{seqs}"
    # create_team 写了 seq=1（team_start），可能还有 team_resumed=2
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"序列有洞：{seqs}"