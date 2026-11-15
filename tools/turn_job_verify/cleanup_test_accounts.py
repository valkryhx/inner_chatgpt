"""清理本轮真机测试写入生产库的测试账号（按 RUN_ID 时间戳圈定）。

## 为什么需要它

8000 端口跑的是**生产库** `backend/data/app.db`。这一轮真机自测（resume_collab /
stale_* 探针 / e2e run.py）各建了 `@e2e.local` 与 `@t.local` 测试账号。

铁律要求「不丢历史数据」，所以：
1. 先确认备份存在；
2. 只删**本轮 RUN_ID 之后**创建的账号（时间戳圈定，不动更早的东西）；
3. 级联删掉它们的会话 / 消息 / 历史 / 团队 / 子代理 / A2A；
4. 删完与备份**逐表比对**，数字必须回到基线。

## 用法

    python tools/turn_job_verify/cleanup_test_accounts.py --since 1794712315 --dry-run
    python tools/turn_job_verify/cleanup_test_accounts.py --since 1794712315

`--since` 是秒级 unix 时间戳；库里 `created_at` 是毫秒。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "backend" / "data" / "app.db"

TABLES_BY_USER = [
    "messages", "session_history", "sessions",
    "team_tasks", "team_messages", "team_events", "team_runs",
    "subagent_messages", "subagent_runs",
    "a2a_audit", "a2a_tokens",
    "user_llm_configs", "user_mcp_servers", "memories_index",
]


def main(argv: list[str]) -> int:
    since_ms = 0
    dry = "--dry-run" in argv
    if "--since" in argv:
        since_ms = int(float(argv[argv.index("--since") + 1]) * 1000)

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    tests = [dict(r) for r in con.execute(
        "select id,email,created_at from users where created_at>=? "
        "and (email like '%@e2e.local' or email like '%@t.local')", (since_ms,))]
    if not tests:
        print("没有需要清理的账号")
        return 0

    ids = [t["id"] for t in tests]
    print(f"待清理 {len(ids)} 个账号（created_at >= {since_ms}）：")
    for t in tests:
        print(f"  {t['id']}  {t['email']}")
    if dry:
        print("\n--dry-run：不改动")
        return 0

    qmarks = ",".join("?" * len(ids))
    for tbl in TABLES_BY_USER:
        try:
            cols = {d[1] for d in con.execute(f"pragma table_info({tbl})")}
        except sqlite3.OperationalError:
            continue
        if "user_id" in cols:
            cur = con.execute(f"delete from {tbl} where user_id in ({qmarks})", ids)
            if cur.rowcount:
                print(f"  {tbl:<20} -{cur.rowcount}")
    cur = con.execute(f"delete from users where id in ({qmarks})", ids)
    print(f"  users                -{cur.rowcount}")
    con.commit()

    print("\n剩余：")
    for t in ("users", "sessions", "messages", "session_history", "team_runs"):
        n = con.execute(f"select count(*) from {t}").fetchone()[0]
        print(f"  {t:<20} {n}")
    left = con.execute(
        "select count(*) from users where email like '%@e2e.local' or email like '%@t.local'"
    ).fetchone()[0]
    print(f"\n残留测试账号：{left}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
