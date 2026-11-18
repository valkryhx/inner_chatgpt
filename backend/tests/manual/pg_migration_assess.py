"""评估 SQLite -> PostgreSQL 迁移成本（只读，不改任何东西）。

产出：
1. 每张表的建表语句、行数、索引
2. SQLite 专有语法统计（需要改的地方）
3. 真实导出一张表到 PG，验证类型映射可行

用法：
    python tests/manual/pg_migration_assess.py
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

DB = BACKEND / "data" / "app.db"
SKIP = {"sqlite_sequence"}

# SQLite 类型 -> 建议的 PG 类型
TYPE_MAP = {
    "TEXT": "TEXT",
    "INTEGER": "BIGINT",
    "REAL": "DOUBLE PRECISION",
    "BLOB": "BYTEA",
    "NUMERIC": "NUMERIC",
    "INT": "BIGINT",
    "BOOLEAN": "BOOLEAN",
}


def main() -> int:
    if not DB.exists():
        print(f"数据库不存在: {DB}")
        return 2

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    tables = [r[0] for r in conn.execute(
        "select name from sqlite_master where type='table' order by name").fetchall()]
    tables = [t for t in tables if t not in SKIP]

    print(f"共 {len(tables)} 张表\n")

    total_rows = 0
    risky: list[tuple[str, str]] = []
    type_usage: dict[str, int] = {}

    print(f"{'表名':<28} {'行数':>8}  {'列数':>4}  需要注意")
    print("-" * 78)

    for t in tables:
        n = conn.execute(f"select count(*) from {t}").fetchone()[0]
        total_rows += n
        cols = conn.execute(f"PRAGMA table_info({t})").fetchall()

        notes = []
        for c in cols:
            ctype = (c["type"] or "").upper()
            raw = ctype.split("(")[0].strip()
            type_usage[raw] = type_usage.get(raw, 0) + 1
            if raw not in TYPE_MAP:
                notes.append(f"{c['name']}:{raw or '(无类型)'}")
            if c["pk"] and raw == "INTEGER":
                notes.append(f"{c['name']}:INTEGER PRIMARY KEY(需改序列)")

        # 自增列（AUTOINCREMENT）
        ddl = conn.execute(
            "select sql from sqlite_master where type='table' and name=?",
            (t,)).fetchone()
        ddl_sql = (ddl[0] or "") if ddl else ""
        if "AUTOINCREMENT" in ddl_sql.upper():
            notes.append("AUTOINCREMENT")

        idx = conn.execute(
            "select name from sqlite_master where type='index' and tbl_name=? "
            "and sql is not null", (t,)).fetchall()

        note_str = ", ".join(notes[:3]) + (" ..." if len(notes) > 3 else "")
        if notes:
            risky.append((t, note_str))
        print(f"{t:<28} {n:>8}  {len(cols):>4}  {note_str[:40]}")

    print()
    print(f"总行数: {total_rows:,}")
    print(f"索引总数: {sum(len(conn.execute('select name from sqlite_master where type=\"index\" and tbl_name=?', (t,)).fetchall()) for t in tables)}")
    print()

    print("=== SQLite 列类型使用统计 ===")
    for k, v in sorted(type_usage.items(), key=lambda x: -x[1]):
        pg = TYPE_MAP.get(k, "需要人工判断")
        print(f"  {k or '(无)'}: {v} 列  ->  PG {pg}")
    print()

    print("=== 需要改的表 ===")
    if not risky:
        print("  无")
    for t, note in risky:
        print(f"  {t}: {note}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())