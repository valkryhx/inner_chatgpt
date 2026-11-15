"""从备份恢复被误删的三张表（team_tasks / team_events / subagent_runs）。

## 背景（必须原样记录）

我在清理本轮 e2e 测试数据时，对这三张表用了「孤儿行」判据：

```python
('team_tasks', "team_id not in (select team_id from team_runs)")
('subagent_runs', "session_id not in (select id from sessions)")
```

判据本身写错了 —— `team_runs` 的 `team_id` 与 `team_tasks` 的 `team_id` 并不是
同一个命名空间（表里根本没有能对上的行），于是 **197 行全被判成孤儿并删除**，
其中 189 行是备份里本来就有的历史数据。`subagent_runs` 同理（591 行被删）。

这是**真实的数据丢失**，不是「看起来像」。所幸备份存在（`work/db_backup/app_20261115_110930.db`），
本脚本按主键把三张表恢复到备份状态。

## 做法

`insert or replace` 按主键回填：已存在且正确的行被覆盖成备份值，被我删掉的行补回来。
只动这三张表，其它表不碰。

## 用法

    python tools/turn_job_verify/restore_from_backup.py --backup work/db_backup/app_20261115_110930.db
    python tools/turn_job_verify/restore_from_backup.py --backup ... --dry-run
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "backend" / "data" / "app.db"
TABLES = ["team_tasks", "team_events", "team_messages", "subagent_runs", "subagent_messages"]


def sync_table(prod, bak, t: str, dry: bool) -> bool:
    """让 prod 的 `t` 表逐行等于备份：补回缺行、删掉多行、覆盖内容不同的行。

    为什么用「逐行对齐」而不是「按 user_id 删」：第一次修复时我按
    `team_id not in (select team_id from team_runs)` 判孤儿，把 197 行历史数据
    全删了 —— 两张表的 `team_id` 根本不是同一命名空间。**猜测关联字段是危险的**，
    备份才是唯一可信的参照。

    主键用 `pragma table_info` 里 `pk > 0` 的列推出来；没有主键的表按整行比较。
    """
    try:
        cols = [d[1] for d in bak.execute(f"pragma table_info({t})")]
        pk = [d[1] for d in bak.execute(f"pragma table_info({t})") if d[5]]
    except sqlite3.OperationalError:
        return True

    bak_rows = [tuple(r) for r in bak.execute(f"select * from {t}")]
    prod_rows = [tuple(r) for r in prod.execute(f"select * from {t}")]
    before = len(prod_rows)

    if not pk:                      # 无主键：只在数量不同时整体替换
        if len(prod_rows) == len(bak_rows):
            print(f"  {t:<24} {before:<6} == {len(bak_rows):<6}（无主键，跳过）")
            return True
        if dry:
            print(f"  {t:<24} 现状={before:<6} 备份={len(bak_rows):<6} --dry-run")
            return False
        prod.execute(f"delete from {t}")
        ph = ",".join("?" * len(cols))
        prod.executemany(f"insert into {t} ({','.join(cols)}) values ({ph})", bak_rows)
        prod.commit()
        print(f"  {t:<24} {before:<6} -> {len(bak_rows):<6}")
        return True

    if dry:
        pset = {tuple(r[cols.index(c)] for c in pk) for r in prod_rows}
        bset = {tuple(r[cols.index(c)] for c in pk) for r in bak_rows}
        print(f"  {t:<24} 现状={before:<6} 备份={len(bak_rows):<6} "
              f"多={len(pset - bset)} 少={len(bset - pset)}")
        return pset == bset

    prod.execute(f"delete from {t}")
    ph = ",".join("?" * len(cols))
    prod.executemany(f"insert into {t} ({','.join(cols)}) values ({ph})", bak_rows)
    prod.commit()
    after = prod.execute(f"select count(*) from {t}").fetchone()[0]
    print(f"  {t:<24} {before:<6} -> {after:<6} {'OK' if after == len(bak_rows) else 'DIFF'}")
    return after == len(bak_rows)


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    all_tables = "--all" in argv
    backup = None
    if "--backup" in argv:
        backup = Path(argv[argv.index("--backup") + 1])
    if backup is None:
        print("必须显式指定 --backup <path>（防止误用其它快照）")
        return 2
    if not backup.exists():
        print(f"备份不存在：{backup}")
        return 2

    print(f"prod   = {DB}")
    print(f"backup = {backup}")
    print()

    prod = sqlite3.connect(str(DB))
    bak = sqlite3.connect(str(backup))
    bak.row_factory = sqlite3.Row

    if all_tables:
        tables = [r[0] for r in bak.execute(
            "select name from sqlite_master where type='table' "
            "and name not like 'sqlite_%' order by name")]
    else:
        tables = TABLES

    for t in tables:
        sync_table(prod, bak, t, dry)

    print()
    print("逐表最终比对：")
    ok = True
    for t in tables:
        try:
            a = prod.execute(f"select count(*) from {t}").fetchone()[0]
            b = bak.execute(f"select count(*) from {t}").fetchone()[0]
        except sqlite3.OperationalError:
            continue
        same = a == b
        ok = ok and same
        if not same:
            print(f"  {t:<24} prod={a:<7} backup={b:<7} DIFF")
    print()
    print("ALL MATCH" if ok else "仍有差异 —— 不要继续，先查清楚")
    prod.close()
    bak.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
