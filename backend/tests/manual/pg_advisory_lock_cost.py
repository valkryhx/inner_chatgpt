"""测量 PG 下 `BEGIN IMMEDIATE` 全局 advisory lock 的串行化代价。

## 要回答的问题

`pgdriver._WRITE_LOCK_KEY` 是**一个固定常量**，所以 6 处 `BEGIN IMMEDIATE`
（converge_generation_facts / create_team / emit_event / consume_inbox /
pin_name / recover_stale_runtime）**互斥**——不管是不是同一个用户、同一个队。

问题是：这个互斥到底多贵？只有在真库上量出来才算数。

## 方法

跑真实的中英混合负载：
- N 个并发协程，每个执行 M 次 `emit_event`（真实 SQL，真实复合主键 + MAX+1）
- 记录总耗时与 p50/p95 单次耗时
- 对照组：把 advisory lock 关掉（模拟「按 team_id 分键」的乐观上限）

之所以要对照组：只看到「有锁时 X ms」不知道锁本身占了多少。

用法：
    python tests/manual/pg_advisory_lock_cost.py --dsn postgresql://...
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))


async def setup_schema(schema: str) -> None:
    """建一份最小的 team_events + 索引。"""
    from app import sqlcompat
    async with sqlcompat.connect(f"/tmp/{schema}/app.db") as conn:
        await conn.executescript("""
CREATE TABLE IF NOT EXISTS team_events (
    team_id       TEXT NOT NULL,
    event_seq     INTEGER NOT NULL,
    user_id       TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    task_id       TEXT NOT NULL DEFAULT '',
    event_type    TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (team_id, event_seq)
)""")
        await conn.commit()


async def emit(scope, conn_factory, *, event_type="t", use_lock=True):
    """与 team_store.emit_event 同构的最小实现。"""
    from app import sqlcompat
    conn = conn_factory()
    try:
        await conn.execute("PRAGMA busy_timeout=5000;")
        if use_lock == "scoped":
            await conn.execute("BEGIN IMMEDIATE " + f"team:{scope[0]}")
        elif use_lock:
            await conn.execute("BEGIN IMMEDIATE")
        else:
            await conn.execute("BEGIN")
        cur = await conn.execute(
            "SELECT COALESCE(MAX(event_seq),0)+1 FROM team_events WHERE team_id=?",
            (scope[0],))
        seq = int((await cur.fetchone())[0])
        await conn.execute(
            "INSERT INTO team_events(team_id,event_seq,user_id,session_id,generation_id,"
            "agent_id,event_type,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (scope[0], seq, "u1", "s1", "g1", "a1", event_type, "{}",
             int(time.time() * 1000)))
        await conn.commit()
    finally:
        await conn.close()


async def run_load(*, workers: int, per_worker: int, distinct_teams: bool,
                   use_lock: bool, schema: str) -> dict:
    from app import sqlcompat

    def factory():
        return sqlcompat.connect(f"/tmp/{schema}/app.db")

    # 同一个 team 让所有 worker 抢同一行区间；distinct 则各写各的队
    scopes = [
        (f"team_{i}" if distinct_teams else "team_shared",)
        for i in range(workers)
    ]
    lat: list[float] = []
    errors = 0

    async def worker(idx: int):
        nonlocal errors
        for _ in range(per_worker):
            t0 = time.perf_counter()
            try:
                await emit(scopes[idx], factory, use_lock=use_lock)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                if errors <= 2:
                    print(f"    err: {type(exc).__name__}: {str(exc)[:70]}")
            lat.append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    await asyncio.gather(*[worker(i) for i in range(workers)])
    elapsed = (time.perf_counter() - t0) * 1000
    lat.sort()
    return {
        "workers": workers, "per_worker": per_worker,
        "total_ops": workers * per_worker,
        "wall_ms": elapsed,
        "throughput": workers * per_worker / (elapsed / 1000),
        "p50": lat[len(lat) // 2] if lat else 0,
        "p95": lat[int(len(lat) * 0.95)] if lat else 0,
        "max": lat[-1] if lat else 0,
        "errors": errors,
    }


def fmt(r: dict) -> str:
    return (f"workers={r['workers']:>2} ops={r['total_ops']:>4} "
            f"wall={r['wall_ms']:>8.1f}ms  {r['throughput']:>7.1f} op/s  "
            f"p50={r['p50']:>7.2f} p95={r['p95']:>7.2f} max={r['max']:>8.2f} "
            f"err={r['errors']}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--schema", default="lockcost")
    args = ap.parse_args()

    os.environ["HLZ_DB_URL"] = args.dsn
    os.environ["HLZ_PG_SCHEMA_PER_PATH"] = "1"
    from app import dbselect, pgdriver, sqlcompat
    dbselect.reset_for_tests()
    await pgdriver.warm_pool()

    schema = dbselect.pg_schema_for(f"/tmp/{args.schema}/app.db")
    await setup_schema(args.schema)

    print(f"schema={schema}\n")
    print("说明：同一个 team_id 时所有 worker 抢同一个 MAX(event_seq)，")
    print("      这正是 advisory lock 要保护的情形。\n")

    for distinct in (True, False):
        label = "各写各的队" if distinct else "全部抢同一个队"
        print(f"=== {label} ===")
        for workers in (1, 4, 16):
            r = await run_load(workers=workers, per_worker=20,
                               distinct_teams=distinct, use_lock=True, schema=args.schema)
            print("  有锁  " + fmt(r))

        # 对照组 1：无锁。同队时必然撞主键，这正好说明「为什么需要锁」
        for workers in (1, 4, 16):
            r = await run_load(workers=workers, per_worker=20,
                               distinct_teams=distinct, use_lock=False, schema=args.schema)
            print("  无锁    " + fmt(r))

        # 对照组 2：分锁（本轮改动）。不同队应接近无锁吞吐，同队仍串行
        for workers in (1, 4, 16):
            r = await run_load(workers=workers, per_worker=20,
                               distinct_teams=distinct, use_lock="scoped", schema=args.schema)
            print("  分锁    " + fmt(r))
        print()

    # 清理
    pool = await pgdriver._get_pool()
    await pool.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    await pgdriver.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))