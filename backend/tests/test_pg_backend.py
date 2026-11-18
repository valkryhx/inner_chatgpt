"""PostgreSQL 后端的真实集成测试。

## 为什么必须真连 PG

适配层的方言改写（`?` → `$n`、`INSERT OR` → `ON CONFLICT`、`PRAGMA` →
`information_schema`、`rowid` → `ctid`）**只能靠真库验证**。单元测试里
再怎么断言字符串，也不证明 PG 会接受它 —— 这正是「构造得出 ≠ 真能跑」。

## 怎么跑

默认**跳过**（除非设了 `HLZ_TEST_PG_DSN`），否则 CI / 没起 PG 的环境会红：

    docker run -d --name hlz-pg-eval -e POSTGRES_PASSWORD=eval123 \\
        -e POSTGRES_USER=hlz -e POSTGRES_DB=hlz -p 15432:5432 postgres:16-bookworm

    $env:HLZ_TEST_PG_DSN="postgresql://hlz:eval123@127.0.0.1:15432/hlz"
    python -m pytest tests/test_pg_backend.py -q

## 隔离

每个用例自己一个 schema（`dbselect.pg_schema_for` 走 `HLZ_PG_SCHEMA_PER_PATH`），
用例退出时 DROP，互不污染，可以并发跑。
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DSN = os.environ.get("HLZ_TEST_PG_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="未设置 HLZ_TEST_PG_DSN，跳过 PG 集成测试"
)


@pytest.fixture()
def pg_env(monkeypatch):
    """把连接指向 PG，并给本用例一个独立 schema。"""
    from app import dbselect, pgdriver

    monkeypatch.setenv("HLZ_DB_URL", DSN)
    monkeypatch.setenv("HLZ_PG_SCHEMA_PER_PATH", "1")
    dbselect.reset_for_tests()

    # 用一个唯一的「假路径」派生出唯一 schema
    fake_path = f"/tmp/hlz_test_{uuid.uuid4().hex}/app.db"
    schema = dbselect.pg_schema_for(fake_path)
    pgdriver.forget_schema_cache()

    yield fake_path, schema

    # 清理：删 schema，关池
    async def _cleanup():
        pool = await pgdriver._get_pool()
        await pool.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await pgdriver.close_pool()

    try:
        asyncio.run(_cleanup())
    except Exception:  # noqa: BLE001 - 清理失败不该让测试红
        pass
    dbselect.reset_for_tests()


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 基础读写

def test_pg_create_insert_select(pg_env):
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript(
                "CREATE TABLE t(id TEXT PRIMARY KEY, v TEXT NOT NULL, n INTEGER NOT NULL)"
            )
            await conn.execute("INSERT INTO t(id,v,n) VALUES(?,?,?)", ("a", "x", 1))
            await conn.commit()
            conn.row_factory = sqlcompat.Row
            cur = await conn.execute("SELECT * FROM t WHERE id=?", ("a",))
            return await cur.fetchone()

    row = run(go())
    assert row["id"] == "a"
    assert row["v"] == "x"
    assert row["n"] == 1


def test_pg_rowcount(pg_env):
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT PRIMARY KEY, v TEXT)")
            await conn.executemany("INSERT INTO t(id,v) VALUES(?,?)",
                                   [("a", "1"), ("b", "2"), ("c", "3")])
            cur = await conn.execute("UPDATE t SET v=? WHERE id<>?", ("9", "a"))
            n = cur.rowcount
            await conn.commit()
            return n

    assert run(go()) == 2


def test_pg_question_mark_inside_string_literal(pg_env):
    """字符串字面量里的 `?` 不能被换成占位符。"""
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT PRIMARY KEY, v TEXT)")
            await conn.execute("INSERT INTO t(id,v) VALUES(?,?)", ("a", "what? really?"))
            await conn.commit()
            conn.row_factory = sqlcompat.Row
            cur = await conn.execute("SELECT v FROM t WHERE v LIKE ?", ("%?%",))
            return (await cur.fetchone())["v"]

    assert run(go()) == "what? really?"


# ---------------------------------------------------------------- upsert

def test_pg_insert_or_replace_becomes_upsert(pg_env):
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript(
                "CREATE TABLE t(id TEXT PRIMARY KEY, v TEXT, n INTEGER)")
            await conn.execute("INSERT INTO t(id,v,n) VALUES(?,?,?)", ("a", "old", 1))
            await conn.commit()
            await conn.execute(
                "INSERT OR REPLACE INTO t(id,v,n) VALUES(?,?,?)", ("a", "new", 2))
            await conn.commit()
            conn.row_factory = sqlcompat.Row
            cur = await conn.execute("SELECT * FROM t WHERE id=?", ("a",))
            return dict(await cur.fetchone())

    row = run(go())
    assert row["v"] == "new"
    assert row["n"] == 2


def test_pg_insert_or_ignore_keeps_existing(pg_env):
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT PRIMARY KEY, v TEXT)")
            await conn.execute("INSERT INTO t(id,v) VALUES(?,?)", ("a", "first"))
            await conn.commit()
            await conn.execute(
                "INSERT OR IGNORE INTO t(id,v) VALUES(?,?)", ("a", "second"))
            await conn.commit()
            conn.row_factory = sqlcompat.Row
            cur = await conn.execute("SELECT v FROM t WHERE id=?", ("a",))
            return (await cur.fetchone())["v"]

    assert run(go()) == "first"


def test_pg_insert_or_replace_composite_pk(pg_env):
    """复合主键的 upsert（session_history / subagent_messages 都是这种）。"""
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript(
                "CREATE TABLE t(a TEXT NOT NULL, b INTEGER NOT NULL, v TEXT, "
                "PRIMARY KEY (a, b))")
            await conn.execute("INSERT INTO t(a,b,v) VALUES(?,?,?)", ("x", 1, "old"))
            await conn.commit()
            await conn.execute(
                "INSERT OR REPLACE INTO t(a,b,v) VALUES(?,?,?)", ("x", 1, "new"))
            await conn.commit()
            conn.row_factory = sqlcompat.Row
            cur = await conn.execute("SELECT v FROM t WHERE a=? AND b=?", ("x", 1))
            return (await cur.fetchone())["v"]

    assert run(go()) == "new"


# ---------------------------------------------------------------- PRAGMA

def test_pg_pragma_table_info_returns_sqlite_shape(pg_env):
    """`PRAGMA table_info(t)` 必须返回与 SQLite 相同的 6 列形状。

    项目里的用法是 `{r[1] for r in rows}` 取列名（a2a / subagent / history
    的旧库迁移都靠它），所以第 2 列必须是列名。
    """
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript(
                "CREATE TABLE t(id TEXT PRIMARY KEY, v TEXT NOT NULL, n INTEGER)")
            cur = await conn.execute("PRAGMA table_info(t)")
            return await cur.fetchall()

    rows = run(go())
    assert len(rows) == 3
    names = [r[1] for r in rows]          # 与项目里的用法一致
    assert names == ["id", "v", "n"]
    # notnull / pk 列也要还原
    by_name = {r[1]: r for r in rows}
    assert by_name["id"][5] == 1          # pk
    assert by_name["v"][3] == 1           # notnull
    assert by_name["n"][3] == 0


def test_pg_noop_pragmas(pg_env):
    """journal_mode / busy_timeout / foreign_keys 在 PG 上必须是空操作。"""
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA busy_timeout=5000;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            await conn.executescript("CREATE TABLE t(id TEXT)")
            return True

    assert run(go()) is True


# ---------------------------------------------------------------- DDL 方言

def test_pg_autoincrement_becomes_identity(pg_env):
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript(
                "CREATE TABLE a (\n"
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                "  v TEXT NOT NULL\n"
                ")"
            )
            await conn.execute("INSERT INTO a(v) VALUES(?)", ("x",))
            await conn.execute("INSERT INTO a(v) VALUES(?)", ("y",))
            await conn.commit()
            conn.row_factory = sqlcompat.Row
            cur = await conn.execute("SELECT id,v FROM a ORDER BY id")
            return [dict(r) for r in await cur.fetchall()]

    rows = run(go())
    assert [r["id"] for r in rows] == [1, 2]


def test_pg_sqlite_master_probe(pg_env):
    """`SELECT 1 FROM sqlite_master WHERE type='table' AND name='x'` 必须成立。"""
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
            cur = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'")
            hit = await cur.fetchone() is not None
            cur = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nope'")
            miss = await cur.fetchone() is None
            return hit, miss

    hit, miss = run(go())
    assert hit is True
    assert miss is True


def test_pg_rowid_becomes_ctid(pg_env):
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript(
                "CREATE TABLE t(id TEXT PRIMARY KEY, ts INTEGER NOT NULL)")
            for i, ts in enumerate([100, 100, 200]):
                await conn.execute("INSERT INTO t(id,ts) VALUES(?,?)", (f"m{i}", ts))
            await conn.commit()
            conn.row_factory = sqlcompat.Row
            # 同 created_at 时用 rowid 兜底排序（list_messages 就是这么写的）
            cur = await conn.execute(
                "SELECT id FROM t ORDER BY ts ASC, rowid ASC")
            return [r["id"] for r in await cur.fetchall()]

    assert run(go()) == ["m0", "m1", "m2"]


# ---------------------------------------------------------------- 事务语义

def test_pg_failed_statement_does_not_abort_transaction(pg_env):
    """init_db 里 `try: ALTER TABLE ADD COLUMN except: pass` 必须继续可用。

    SQLite 里失败语句不影响后续；PG 里会让整个事务进入 aborted 态。
    适配层用 SAVEPOINT 解决，这条测试就是守它。
    """
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT)")
            # 故意失败：列已存在 / 表不存在
            for bad in ("ALTER TABLE t ADD COLUMN id TEXT",
                        "ALTER TABLE nope ADD COLUMN x TEXT"):
                try:
                    await conn.execute(bad)
                except Exception:  # noqa: BLE001
                    pass
            # 后续语句必须还能跑
            await conn.execute("INSERT INTO t(id) VALUES(?)", ("ok",))
            await conn.commit()
            cur = await conn.execute("SELECT count(*) FROM t")
            return (await cur.fetchone())[0]

    assert run(go()) == 1


def test_pg_uncommitted_rolls_back_on_exit(pg_env):
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT)")
        async with sqlcompat.connect(path) as conn:
            await conn.execute("INSERT INTO t(id) VALUES(?)", ("x",))
            # 不 commit
        async with sqlcompat.connect(path) as conn:
            cur = await conn.execute("SELECT count(*) FROM t")
            return (await cur.fetchone())[0]

    assert run(go()) == 0


def test_pg_integrity_error_catchable(pg_env):
    path, _ = pg_env
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT PRIMARY KEY)")
            await conn.execute("INSERT INTO t(id) VALUES(?)", ("a",))
            await conn.commit()
            try:
                await conn.execute("INSERT INTO t(id) VALUES(?)", ("a",))
                await conn.commit()
            except sqlcompat.IntegrityError:
                return True
            return False

    assert run(go()) is True


def test_pg_integrity_error_catchable_as_aiosqlite(pg_env):
    """现有代码写的是 `except aiosqlite.IntegrityError`，PG 下也得抓到。"""
    path, _ = pg_env
    import aiosqlite
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(path) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT PRIMARY KEY)")
            await conn.execute("INSERT INTO t(id) VALUES(?)", ("a",))
            await conn.commit()
            try:
                await conn.execute("INSERT INTO t(id) VALUES(?)", ("a",))
                await conn.commit()
            except aiosqlite.IntegrityError:
                return True
            return False

    assert run(go()) is True


# ---------------------------------------------------------------- 真实 DAO 层

def test_pg_real_db_module_roundtrip(pg_env):
    """把 app.db 的真实 DAO 跑在 PG 上：建表 → 建会话 → 建消息 → 查回。"""
    path, schema = pg_env
    from app import db

    async def go():
        import app.db as dbmod
        orig = dbmod.DB_PATH
        dbmod.DB_PATH = path
        try:
            await db.init_db()
            s = await db.create_session("u1", title="会话一")
            await db.add_message(s["id"], "user", content="你好")
            await db.add_message(s["id"], "assistant", content="在的")
            msgs = await db.list_messages(s["id"])
            sessions, _ = await db.list_sessions("u1")
            got = await db.get_session(s["id"], "u1")
            denied = await db.get_session(s["id"], "u2")
            return len(msgs), len(sessions), got["title"], denied
        finally:
            dbmod.DB_PATH = orig

    n_msgs, n_sess, title, denied = run(go())
    assert n_msgs == 2
    assert n_sess == 1
    assert title == "会话一"
    assert denied is None      # 多用户隔离必须继续成立


def test_pg_real_team_store_roundtrip(pg_env):
    """team_store 的建表 + 事件序列（用了 COALESCE(MAX(...)) 和复合主键）。"""
    path, _ = pg_env
    from app import team_store

    async def go():
        import app.team_store as mod
        orig = mod.DB_PATH
        mod.DB_PATH = path
        try:
            await team_store.init_tables()
            async with __import__("app.sqlcompat", fromlist=["x"]).connect(path) as conn:
                conn.row_factory = None
                cur = await conn.execute("SELECT count(*) FROM team_runs")
                return (await cur.fetchone())[0]
        finally:
            mod.DB_PATH = orig

    assert run(go()) == 0


def test_pg_real_subagent_store_seq(pg_env):
    """subagent 的 seq 是调用方给号 + INTEGER 复合主键，写读顺序必须保持。"""
    path, _ = pg_env
    from app.subagent import store

    async def go():
        orig = store.DB_PATH
        store.DB_PATH = path
        try:
            await store.init_tables()
            await store.create_run(
                run_id="r1", ctx_user_id="u1", session_id="s1", message_id="m1",
                parent_agent_id="root", agent_id="a1", name="n", role="member",
                task_message="t")
            start = await store.append_sidechain(
                "r1", [{"role": "user", "content": "hi"},
                       {"role": "assistant", "content": "yo"}],
                user_id="u1", session_id="s1", generation_id="", team_id="")
            got = await store.load_sidechain("r1", user_id="u1", session_id="s1")
            return start, got
        finally:
            store.DB_PATH = orig

    start, got = run(go())
    # append_sidechain 返回「下一条该用的 seq」，写了两条所以是 2
    assert start == 2
    assert [m["content"] for m in got] == ["hi", "yo"]