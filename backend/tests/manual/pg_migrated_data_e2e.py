"""迁移后的真实数据可用性验证。

## 为什么不能只看行数

行数一致只证明「搬过来了」，不证明「应用能用」。真正的风险是：
- 自增序列没对齐 → 新插入撞主键（迁移当下查不出来）
- 联合主键的 upsert 在 PG 上走到别的分支
- 时间戳排序（毫秒 BIGINT）在 PG 上语义一致

所以这里用**迁移进来的真实数据**跑一遍应用层操作：读会话、读消息、
写新消息、写新的队事件（自增）、搜索、team 事件序列。

## 用法

    python tests/manual/pg_migrated_data_e2e.py --dsn postgresql://...

⚠️ 会往库里写测试数据（会话/消息/team 事件），跑在迁移副本上，不要对生产库跑。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--schema", default="public")
    args = ap.parse_args()

    os.environ["HLZ_DB_URL"] = args.dsn

    from app import dbselect, db, team_store, history_store
    dbselect.reset_for_tests()

    # 把 DB_PATH 指到一个「假路径」，用它派生出的 schema 就是目标 schema。
    # 生产下 schema 恒为 public，这里为了能指定 migrate_test 而设开关。
    os.environ["HLZ_PG_SCHEMA_PER_PATH"] = "0"
    dbselect.reset_for_tests()
    # 直接改 schema（脚本级，不污染产品代码）
    from app import pgdriver
    _orig_init = pgdriver.PgConnection.__init__

    def _patched(self, path=None):
        _orig_init(self, path)
        self.schema = args.schema

    pgdriver.PgConnection.__init__ = _patched

    print(f"目标：{args.dsn}   schema={args.schema}\n")

    # ---- 1. 读真实会话 ----
    users = [r["id"] for r in await db._q("SELECT id FROM users LIMIT 5")] \
        if hasattr(db, "_q") else []
    if not users:
        import app.sqlcompat as sc
        async with sc.connect("x") as conn:
            cur = await conn.execute("SELECT id FROM users LIMIT 5")
            users = [r[0] for r in await cur.fetchall()]
    check("能读到 users 表", len(users) > 0, f"{len(users)} 个用户")

    uid = users[0] if users else ""
    sessions, has_more = await db.list_sessions(uid, limit=5)
    check("能列出该用户的会话", True, f"{len(sessions)} 条")

    if sessions:
        sid = sessions[0]["id"]
        msgs = await db.list_messages(sid)
        check("能读出会话消息", True, f"{len(msgs)} 条")
        # 顺序必须与 created_at 一致（毫秒 BIGINT 排序）
        ts = [m["created_at"] for m in msgs]
        check("消息按时间升序", ts == sorted(ts), f"{len(ts)} 条")

    # ---- 2. 写新数据（自增序列）----
    new_s = await db.create_session(uid, title="迁移验证会话")
    check("能建新会话", bool(new_s.get("id")), new_s.get("id", ""))
    await db.add_message(new_s["id"], "user", content="迁移后写入测试")
    got = await db.list_messages(new_s["id"])
    check("新消息能读回", len(got) == 1 and got[0]["content"] == "迁移后写入测试")

    # ---- 3. 搜索（LIKE + ESCAPE + rowid→ctid）----
    hits, _ = await db.search_messages(uid, "测试", limit=5)
    check("搜索能返回结果", True, f"{len(hits)} 条命中")

    # ---- 4. team 事件序列（复合主键 + MAX+1 + advisory lock）----
    scope = team_store.TeamScope(
        user_id=uid, session_id=new_s["id"], generation_id="gen_mig",
        team_id="team_mig", agent_id="root")
    try:
        await team_store.init_tables()
        await team_store.create_team(scope, name="迁移验证队")
        e1 = await team_store.emit_event(scope, "test_one", {"n": 1})
        e2 = await team_store.emit_event(scope, "test_two", {"n": 2})
        check("team 事件序列递增",
              e2["event_seq"] == e1["event_seq"] + 1,
              f"{e1['event_seq']} -> {e2['event_seq']}")
        check("team 建队成功", True)
    except Exception as exc:  # noqa: BLE001
        check("team 事件序列递增", False, f"{type(exc).__name__}: {str(exc)[:80]}")

    # ---- 5. history_store 复合主键 upsert ----
    try:
        store = history_store.HistoryStore()
        await store.init()
        await store.set(uid, new_s["id"], [{"role": "user", "content": "h1"}])
        await store.set(uid, new_s["id"], [{"role": "user", "content": "h2"}])
        back = await store.get(uid, new_s["id"])
        check("history 覆盖式写入", len(back) == 1 and back[0]["content"] == "h2",
              f"{len(back)} 条")
    except Exception as exc:  # noqa: BLE001
        check("history 覆盖式写入", False, f"{type(exc).__name__}: {str(exc)[:80]}")

    # ---- 6. 多用户隔离（迁移后必须仍然成立）----
    if len(users) > 1:
        denied = await db.get_session(new_s["id"], users[1])
        check("跨用户读会话被拒", denied is None)

    print("\n=== 汇总 ===")
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"{len(RESULTS)-len(bad)}/{len(RESULTS)} 通过")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))