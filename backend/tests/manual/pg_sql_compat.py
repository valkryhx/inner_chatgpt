"""验证代码里现有的 SQL 写法在 PostgreSQL 上能不能跑。

这是迁移的真正风险点 —— 表能建、数据能导，但**查询写法不兼容**才是
真正花时间的地方。

SQLite 与 PostgreSQL 的语义差异：
- `INSERT OR REPLACE`     -> PG 没有，要改 `ON CONFLICT ... DO UPDATE`
- `INSERT OR IGNORE`      -> PG 用 `ON CONFLICT ... DO NOTHING`
- `ON CONFLICT(...) DO UPDATE SET x=excluded.x` -> PG 写法相同，但
  大小写敏感：`excluded` 在 PG 里是保留字，用小写可以，但表名要引号
- 隐式类型转换：SQLite 允许 `WHERE id = '123'`（字符串比整数），PG 报错
- `?` 占位符             -> PG 用 $1, $2（驱动层的事，不是 SQL 本身）

用法：
    python tests/manual/pg_sql_compat.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

PG_DSN = "postgresql://hlz:eval123@127.0.0.1:15432/hlz"

TESTS: list[tuple[str, str]] = [
    # (说明, SQL)
    ("INSERT OR REPLACE（SQLite 专有）",
     "INSERT OR REPLACE INTO t_kv(k, v) VALUES ($1, $2)"),

    ("INSERT OR IGNORE（SQLite 专有）",
     "INSERT OR IGNORE INTO t_kv(k, v) VALUES ($1, $2)"),

    ("ON CONFLICT DO UPDATE（两库通用）",
     'INSERT INTO t_kv(k, v) VALUES ($1, $2) '
     'ON CONFLICT(k) DO UPDATE SET v = excluded.v'),

    ("ON CONFLICT DO NOTHING（两库通用）",
     'INSERT INTO t_kv(k, v) VALUES ($1, $2) ON CONFLICT(k) DO NOTHING'),

    ("隐式类型转换：字符串比整数",
     "SELECT 1 WHERE 123 = '123'"),

    ("动态类型：TEXT 列存数字再比较",
     "SELECT 1 WHERE '10' > '9'"),

    ("GROUP BY 后直接选非聚合列（SQLite 宽松）",
     "SELECT k, v FROM t_kv GROUP BY k"),

    ("双引号当字符串（SQLite 兼容，PG 视为标识符）",
     'SELECT 1 WHERE "some_string" = 1'),
]

SETUP = [
    "DROP TABLE IF EXISTS t_kv",
    "CREATE TABLE t_kv (k TEXT PRIMARY KEY, v TEXT)",
]


async def main() -> int:
    try:
        import asyncpg
    except ImportError:
        print("缺 asyncpg")
        return 2

    try:
        pg = await asyncpg.connect(PG_DSN)
    except Exception as exc:  # noqa: BLE001
        print(f"连不上 PG：{exc}")
        return 2

    try:
        for stmt in SETUP:
            await pg.execute(stmt)

        print("=== 现有 SQL 写法在 PG 上的兼容性 ===\n")
        print(f"{'写法':<42} {'结果':<8} 说明")
        print("-" * 100)

        for desc, sql in TESTS:
            params = ("k1", "v1") if "$1" in sql else None
            try:
                if params:
                    await pg.execute(sql, *params)
                else:
                    await pg.fetch(sql)
                print(f"{desc:<42} {'OK':<8}")
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).split("\n")[0][:60]
                print(f"{desc:<42} {'FAIL':<8} {msg}")

        print()
        print("=== 结论 ===")
        print("  OK   = 可以直接用，不用改")
        print("  FAIL = 需要改写")

    finally:
        try:
            await pg.execute("DROP TABLE IF EXISTS t_kv")
        except Exception:
            pass
        await pg.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))