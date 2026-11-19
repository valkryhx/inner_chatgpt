"""实测：新实例启动时 `db.init_db()` 会不会误伤**别的实例正在跑**的生成。

## 怀疑

`backend/app/db.py:194-196` 有一条**无条件**的启动自愈：

```python
await conn.execute(
    "UPDATE messages SET status='stopped' WHERE role='assistant' AND status='streaming'"
)
```

它没有任何 owner / leader / live_owners 过滤。单进程时这是对的（进程刚启动不可能有
生成在跑）。但多实例下 `init_db()` 在**每个实例的 lifespan 里都执行**（`main.py:48`）。
于是扩容一个新副本 = 把现有副本正在跑的会话全部标成 stopped。

## 这会怎样

1. 消息 status 被改成 `stopped` → 那条 assistant 消息在前端变成「已停止」
2. 生成其实还在跑；它之后照常写 blocks，但状态已经被判死
3. `stop_generation` / 续看 / 收敛逻辑全部基于这个字段

## 判据（必须是「本实例自己没有生成」而不是「没人有生成」）

只看本进程的 `turn_job._JOBS` 是否为空 —— 那才是「我这里不可能有生成在跑」。

## 数据库隔离（重要）

**不能依赖 `HLZ_DB_PATH`** —— `DB_PATH` 是 `app/config.py:25` 的模块级常量，
import 时就定了，环境变量改不动它。本仓库所有测试的隔离办法是
`monkeypatch.setattr(db, "DB_PATH", tmp_path)`。

第一版探针就是因为误以为有 `HLZ_DB_PATH`，直接往真实库 `backend/data/app.db`
写进了 u_test / s_test / m_test 三条数据（发现后已清理）。**这类探针必须先确认
写的是哪个库**，否则「验证」本身就变成了事故。

## 两种后端

```bash
# SQLite（默认，无需容器）
python tests/manual/probe_initdb_cross_instance_wipe.py

# PostgreSQL（隔离靠 HLZ_PG_SCHEMA_PER_PATH，见 dbselect.py:126）
HLZ_PG_SCHEMA_PER_PATH=1 \
HLZ_DB_URL="postgresql://hlz:<pw>@127.0.0.1:15432/hlz" \
  python tests/manual/probe_initdb_cross_instance_wipe.py
```
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


async def main() -> int:
    pg_url = (os.environ.get("HLZ_DB_URL") or "").strip()
    use_pg = pg_url.startswith("postgresql")

    if use_pg and (os.environ.get("HLZ_PG_SCHEMA_PER_PATH") or "").strip() in (
            "", "0", "false", "False"):
        print("!! PG 模式必须设 HLZ_PG_SCHEMA_PER_PATH=1，否则写进 public 会毁掉真实数据")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="hlz_initdb_race_"))
    db_file = tmp / "probe.db"
    pg_db_path = tmp / "probe.pg"     # PG 模式下只用它的路径字符串哈希出独立 schema

    from app import db, sqlcompat

    # —— 隔离：与 tests/ 里 monkeypatch.setattr(db, "DB_PATH", tmp) 同款 ——
    # 这是唯一可靠的隔离手段；环境变量改不动模块级常量。
    old_db_path = db.DB_PATH
    target = db_file if not use_pg else pg_db_path
    db.DB_PATH = target

    # team_store / turn_job 等模块各自也 import 了 DB_PATH，需要同步
    touched: list[tuple[object, object]] = []
    for modname in ("team_store", "turn_job", "subagent.store", "turn_marker"):
        try:
            mod = __import__(f"app.{modname}", fromlist=["DB_PATH"])
        except Exception:
            continue
        if hasattr(mod, "DB_PATH"):
            touched.append((mod, mod.DB_PATH))
            mod.DB_PATH = target

    try:
        print(f"后端 = {'PostgreSQL' if use_pg else 'SQLite'}；隔离路径 = {target}")

        # 防呆：绝不能是真实库
        real = (BACKEND / "data/app.db").resolve()
        try:
            same = Path(str(target)).resolve() == real
        except Exception:
            same = False
        if same:
            print("!! 目标路径就是真实库，拒绝执行")
            return 2

        await db.init_db()

        user_id, session_id, msg_id = "u_probe", "s_probe", "m_probe"

        async with sqlcompat.connect(target) as conn:
            await conn.execute(
                "INSERT INTO users(id,email,name,username,password_hash,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (user_id, "probe@test.invalid", "probe", "probe_user", "x", db.now_ms()),
            )
            await conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (session_id, user_id, "probe", db.now_ms(), db.now_ms()),
            )
            await conn.execute(
                "INSERT INTO messages(id,session_id,role,content,created_at,status) "
                "VALUES(?,?,?,?,?,?)",
                (msg_id, session_id, "assistant", "", db.now_ms(), "streaming"),
            )
            await conn.commit()

        async def read_status() -> str:
            async with sqlcompat.connect(target) as conn:
                cur = await conn.execute("SELECT status FROM messages WHERE id=?", (msg_id,))
                row = await cur.fetchone()
            return str(row[0]) if row else "<missing>"

        before = await read_status()
        print(f"实例 A 正在跑：status = {before!r}")

        # 模拟「实例 B 启动」：再跑一次 init_db（新进程会这么做）
        from app import turn_job
        assert not turn_job._JOBS, "本进程应当没有 job（模拟新实例）"
        await db.init_db()

        after = await read_status()
        print(f"实例 B 启动后：status = {after!r}")
        print()

        ok = after == "streaming"
        if ok:
            print("PASS：新实例启动没有误伤别的实例正在跑的生成")
        else:
            print("FAIL：新实例启动把别的实例正在跑的生成标成了 stopped")
            print("      根因：db.py:194-196 的启动自愈没有 owner / leader 过滤")
        return 0 if ok else 1
    finally:
        db.DB_PATH = old_db_path
        for mod, old in touched:
            mod.DB_PATH = old


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))