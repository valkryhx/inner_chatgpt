"""SQL 适配层的行为测试（不依赖 PG，可离线跑）。

## 为什么这些测试值得存在

适配层要替代 126 处直接 `aiosqlite.connect`，任何语义偏差都会被放大到
全仓。所以这里测的是**调用方真实依赖的行为**，不是实现细节：

1. SQLite 后端行为必须与直接用 aiosqlite 完全一致
   （`row_factory` / `fetchone` / `rowcount` / `executescript` / `async with`）
2. SQL 方言改写必须**只改该改的**
   （字符串里的 `?` 不能被换、注释里的 `?` 不能被换）
3. 语句拆分必须能处理 `DEFAULT ';'` 这种字面量里的分号

反向确认（AGENTS.md §8.5）：把 `_rewrite_placeholders` 里的字面量跳过逻辑
删掉，`test_string_literal_question_mark_preserved` 必须立刻变红。
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import sqlcompat  # noqa: E402
from app.sqlcompat import Row, _rewrite_placeholders, split_statements  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 占位符改写

def test_rewrite_placeholders_basic():
    assert _rewrite_placeholders("INSERT INTO t(a,b) VALUES(?,?)") == \
        "INSERT INTO t(a,b) VALUES($1,$2)"


def test_string_literal_question_mark_preserved():
    """字符串字面量里的 `?` 是数据，不是占位符。"""
    sql = "SELECT * FROM t WHERE v = 'a?b' AND w = ?"
    assert _rewrite_placeholders(sql) == \
        "SELECT * FROM t WHERE v = 'a?b' AND w = $1"


def test_escaped_quote_in_literal():
    """`''` 是转义单引号，不能提前结束字面量。"""
    sql = "SELECT * FROM t WHERE v = 'it''s ?' AND w = ?"
    assert _rewrite_placeholders(sql) == \
        "SELECT * FROM t WHERE v = 'it''s ?' AND w = $1"


def test_line_comment_question_mark_preserved():
    sql = "SELECT 1 -- what? really?\nWHERE x = ?"
    out = _rewrite_placeholders(sql)
    assert "what? really?" in out
    assert out.endswith("WHERE x = $1")


def test_block_comment_question_mark_preserved():
    sql = "SELECT /* why? */ 1 WHERE x = ?"
    out = _rewrite_placeholders(sql)
    assert "why?" in out
    assert out.endswith("$1")


def test_double_quoted_identifier_with_question_mark():
    sql = 'SELECT "has?q" FROM t WHERE x = ?'
    out = _rewrite_placeholders(sql)
    assert '"has?q"' in out
    assert out.endswith("$1")


# ---------------------------------------------------------------- 语句拆分

def test_split_statements_simple():
    assert split_statements("CREATE TABLE a(x); CREATE TABLE b(y)") == [
        "CREATE TABLE a(x)", "CREATE TABLE b(y)"]


def test_split_statements_semicolon_in_literal():
    """`DEFAULT ';'` 里的分号不是语句分隔。"""
    sql = "CREATE TABLE a(x TEXT DEFAULT ';'); CREATE INDEX i ON a(x)"
    out = split_statements(sql)
    assert len(out) == 2
    assert "DEFAULT ';'" in out[0]


def test_split_statements_trailing_semicolon_no_empty():
    assert split_statements("CREATE TABLE a(x);") == ["CREATE TABLE a(x)"]


def test_split_statements_empty():
    assert split_statements("") == []
    assert split_statements("   \n  ") == []


# ---------------------------------------------------------------- Row 兼容面

def test_row_access_by_index_name_and_dict():
    r = Row(["id", "Name"], ("a", "b"))
    assert r[0] == "a"
    assert r["id"] == "a"
    assert r["name"] == "b"          # 列名大小写不敏感（同 sqlite3.Row）
    assert r["NAME"] == "b"
    assert dict(zip(r.keys(), r)) == {"id": "a", "Name": "b"}


def test_row_missing_column_raises_index_error():
    r = Row(["id"], ("a",))
    with pytest.raises(IndexError):
        r["nope"]


def test_row_len_and_iter():
    r = Row(["a", "b"], (1, 2))
    assert len(r) == 2
    assert list(r) == [1, 2]


# ---------------------------------------------------------------- SQLite 后端

@pytest.fixture()
def dbpath(tmp_path):
    return tmp_path / "t.db"


def test_sqlite_executescript_and_query(dbpath):
    async def go():
        async with sqlcompat.connect(dbpath) as conn:
            await conn.executescript(
                "CREATE TABLE t(id TEXT PRIMARY KEY, v TEXT);"
                "CREATE INDEX i ON t(v);"
            )
            await conn.execute("INSERT INTO t(id,v) VALUES(?,?)", ("a", "1"))
            await conn.commit()
            conn.row_factory = sqlcompat.Row
            cur = await conn.execute("SELECT * FROM t")
            return await cur.fetchall()
    rows = run(go())
    assert len(rows) == 1
    assert rows[0]["id"] == "a"


def test_sqlite_rowcount_on_update(dbpath):
    async def go():
        async with sqlcompat.connect(dbpath) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT PRIMARY KEY, v TEXT)")
            await conn.executemany("INSERT INTO t(id,v) VALUES(?,?)",
                                   [("a", "1"), ("b", "2")])
            cur = await conn.execute("UPDATE t SET v='9'")
            n = cur.rowcount
            await conn.commit()
            return n
    assert run(go()) == 2


def test_sqlite_awaitable_execute_matches_aiosqlite(dbpath):
    """`cur = await conn.execute(...)` 与 `async with conn.execute(...)` 都要能用。"""
    async def go():
        async with sqlcompat.connect(dbpath) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT)")
            await conn.execute("INSERT INTO t VALUES(?)", ("a",))
            conn.row_factory = sqlcompat.Row
            async with conn.execute("SELECT id FROM t") as cur:
                a = await cur.fetchone()
            cur2 = await conn.execute("SELECT id FROM t")
            b = await cur2.fetchone()
            return dict(a), dict(b)
    a, b = run(go())
    assert a == b == {"id": "a"}


@pytest.mark.skipif(
    __import__("app.dbselect", fromlist=["x"]).is_postgres(),
    reason="该用例用 sqlite3 直读文件验证回滚，PG 后端无文件可读",
)
def test_sqlite_uncommitted_rolls_back_on_exit(dbpath):
    """`async with` 退出时不 commit 就回滚 —— 与 aiosqlite 一致。"""
    async def go():
        async with sqlcompat.connect(dbpath) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT)")
        async with sqlcompat.connect(dbpath) as conn:
            await conn.execute("INSERT INTO t VALUES(?)", ("x",))
            # 不 commit
        sync = sqlite3.connect(str(dbpath))
        try:
            return sync.execute("SELECT count(*) FROM t").fetchone()[0]
        finally:
            sync.close()
    assert run(go()) == 0


def test_sqlite_pragma_passthrough(dbpath):
    """SQLite 后端必须原样透传 PRAGMA（init_db 依赖它做旧库迁移）。"""
    async def go():
        async with sqlcompat.connect(dbpath) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT NOT NULL)")
            cur = await conn.execute("PRAGMA table_info(t)")
            return await cur.fetchall()
    rows = run(go())
    assert len(rows) == 1
    assert rows[0][1] == "id"        # 第 2 列是列名


def test_sqlite_integrity_error_catchable_via_sqlcompat(dbpath):
    """`except sqlcompat.IntegrityError` 必须能抓到 SQLite 的约束违规。"""
    async def go():
        dbpath.parent.mkdir(parents=True, exist_ok=True)
        async with sqlcompat.connect(dbpath) as conn:
            await conn.executescript("CREATE TABLE t(id TEXT PRIMARY KEY)")
            await conn.execute("INSERT INTO t VALUES(?)", ("a",))
            await conn.commit()
            try:
                await conn.execute("INSERT INTO t VALUES(?)", ("a",))
                await conn.commit()
            except sqlcompat.IntegrityError:
                return True
            return False
    assert run(go()) is True


@pytest.mark.skipif(
    __import__("app.dbselect", fromlist=["x"]).is_postgres(),
    reason="await connect() 只在 SQLite 后端支持，PG 的池化连接不实现 __await__",
)
def test_sqlite_connect_await_form(dbpath):
    """`conn = await connect(path)` 这种写法也要成立。"""
    async def go():
        conn = await sqlcompat.connect(dbpath)
        await conn.executescript("CREATE TABLE t(id TEXT)")
        await conn.close()
        return True
    assert run(go()) is True


# ---------------------------------------------------------------- 后端选择

def test_backend_defaults_to_sqlite(monkeypatch):
    monkeypatch.delenv("HLZ_DB_URL", raising=False)
    from app import dbselect
    dbselect.reset_for_tests()
    assert dbselect.is_postgres() is False
    assert sqlcompat.connect("x.db").backend == "sqlite"
    dbselect.reset_for_tests()


def test_backend_switches_on_postgres_url(monkeypatch):
    monkeypatch.setenv("HLZ_DB_URL", "postgresql://u:p@h:5432/d")
    from app import dbselect
    dbselect.reset_for_tests()
    try:
        assert dbselect.is_postgres() is True
        assert sqlcompat.connect("x.db").backend == "postgres"
    finally:
        dbselect.reset_for_tests()


@pytest.mark.parametrize("url,expected", [
    ("", None),
    ("sqlite:///D:/x/app.db", "D:/x/app.db"),
    ("postgres://u:p@h/d", None),
    ("postgresql+asyncpg://u:p@h/d", None),
])
def test_resolve_sqlite_path(monkeypatch, url, expected):
    from app import dbselect
    monkeypatch.setenv("HLZ_DB_URL", url)
    dbselect.reset_for_tests()
    try:
        got = dbselect.resolve_sqlite_path("/default/app.db")
        assert str(got).replace("\\", "/") == (expected or "/default/app.db")
    finally:
        dbselect.reset_for_tests()


def test_pg_dsn_normalizes_scheme(monkeypatch):
    from app import dbselect
    monkeypatch.setenv("HLZ_DB_URL", "postgres+asyncpg://u:p@h:5432/d")
    dbselect.reset_for_tests()
    try:
        assert dbselect.pg_dsn() == "postgresql://u:p@h:5432/d"
    finally:
        dbselect.reset_for_tests()