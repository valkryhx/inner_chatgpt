"""数据库驱动适配层：让调用方只写一种 SQL，两个后端都能跑。

## 为什么要有这一层

全仓有 126 处 `aiosqlite.connect(...)` 和 58 处 `PRAGMA`，散在 10 个模块里。
要迁到 PostgreSQL 有两条路：

1. 在 126 处逐个改 —— 改动面巨大，且每改一处都可能引入回归，没有测试能兜住。
2. **在驱动层做适配** —— 调用方写 `sqlcompat.connect(path)`，
   SQLite 后端原样透传（零行为变化），PG 后端把 SQL 方言翻译过去。

选 2。理由是：**SQLite 路径必须一字不改地保持现状**（它是当前生产库，
1242 条测试是唯一可信的回归网），而 PG 方言的差异是**有穷的、可枚举的**
（`?` 占位符、`INSERT OR REPLACE/IGNORE`、`PRAGMA`、`rowid`），
集中在一处翻译比散在 126 处改可靠得多。

## 调用方接口（与 `aiosqlite` 完全一致）

    async with connect(path) as conn:
        conn.row_factory = Row          # 可选
        cur = await conn.execute(sql, params)   # 也可以 async with conn.execute(...)
        row = await cur.fetchone()      # 支持 r[0] / r["name"] / dict(r)
        await conn.commit()
        await conn.executescript(schema)
        await conn.executemany(sql, rows)

## 方言翻译（只对 PG 后端生效）

| SQLite 写法 | PG 处理 |
| --- | --- |
| `?` 占位符 | 按顺序换成 `$1..$n`（跳过字符串字面量与注释） |
| `INSERT OR REPLACE INTO t(c..)` | `INSERT ... ON CONFLICT(pk) DO UPDATE SET ...`（主键从 PG 元数据查，带缓存） |
| `INSERT OR IGNORE INTO t(c..)` | `INSERT ... ON CONFLICT DO NOTHING` |
| `PRAGMA table_info(t)` | 查 `information_schema` 还原成 SQLite 的 6 列形状 |
| `PRAGMA journal_mode/busy_timeout/foreign_keys` | 空操作（PG 不需要） |
| `ORDER BY x.rowid` | `ORDER BY x.ctid`（物理行序，语义最接近） |
| `executescript(sql)` | 按 `;` 拆开逐条执行（DDL 里没有函数体，简单拆分安全） |
| `aiosqlite.IntegrityError` | 把 PG 的 Unique/ForeignKey/NotNull/Check 违规映射成 `IntegrityError` |

## 事务语义

SQLite 是「execute 后进入隐式事务，直到 commit」。PG 后端用 autocommit 模拟
**同一件事**：每条语句自带事务，`commit()`/`rollback()` 变成显式事务的开关。
这样 `try: ALTER TABLE ... except: pass` 这种「故意失败继续跑」的迁移代码
在 PG 上行为一致 —— 失败只回滚那一条，不会把整个事务打成 aborted。
需要真原子性的地方用 `async with conn.transaction():`。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# 后端名：sqlite | postgres
BACKEND_SQLITE = "sqlite"
BACKEND_POSTGRES = "postgres"

# 约束违规异常：**直接复用 sqlite3 的那个**，不新造一个。
#
# 这样三件事同时成立，现有代码一行不用改：
#   1. `except aiosqlite.IntegrityError` 在 SQLite 后端照常（aiosqlite 原样抛）
#   2. `except aiosqlite.IntegrityError` 在 PG 后端也能抓到
#      （`pgdriver` 把 asyncpg 的 UniqueViolation 等映射成本类）
#   3. 新代码写 `except sqlcompat.IntegrityError` 两个后端通吃
IntegrityError = sqlite3.IntegrityError


class Row:
    """与 `sqlite3.Row` **等价接口**的行对象（PG 后端用）。

    调用方按三种方式用它，都得支持：
        r[0]                按列序
        r["name"]           按列名（大小写不敏感，同 sqlite3.Row）
        dict(r)             转字典

    ⚠️ 这个类只在 PG 后端用。SQLite 后端由 `sqlitedriver` 把
    `conn.row_factory = Row` 换成真正的 `sqlite3.Row` —— 因为 aiosqlite 的
    row_factory 是 `(cursor, row) -> row` 两参数签名，和这个三参数构造不同。
    调用方不需要关心，写 `conn.row_factory = sqlcompat.Row` 就行。
    """

    __slots__ = ("_names", "_values", "_index")

    def __init__(self, names: list[str], values: tuple) -> None:
        self._names = names
        self._values = values
        self._index = {n.lower(): i for i, n in enumerate(names)}

    def keys(self) -> list[str]:
        return list(self._names)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return self._values[self._index[key.lower()]]
            except KeyError:
                raise IndexError(f"no such column: {key}") from None
        return self._values[key]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self):
        return iter(self._values)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._names, self._values))!r})"


# ---------------------------------------------------------------- SQL 预处理

_SQL_TOKEN = re.compile(
    r"""
      (?:'(?:[^']|'')*')          # 单引号字符串（'' 是转义）
    | (?:"(?:[^"]|"")*")          # 双引号标识符
    | (?:--[^\n]*)                # 行注释
    | (?:/\*.*?\*/)               # 块注释
    | \?                          # 位置占位符
    """,
    re.VERBOSE | re.DOTALL,
)


def _rewrite_placeholders(sql: str) -> str:
    """把 `?` 换成 PG 的 `$n`，**跳过字符串字面量与注释**。

    不能直接 `sql.replace("?", "$1")` —— `WHERE v='a?b'` 里的问号会被误替换，
    这在 `LIKE` 查询里是真实风险（项目里就有 `ESCAPE '\\'` 的搜索）。
    """
    n = 0
    out: list[str] = []
    pos = 0
    for m in _SQL_TOKEN.finditer(sql):
        out.append(sql[pos:m.start()])
        tok = m.group(0)
        if tok == "?":
            n += 1
            out.append(f"${n}")
        else:
            out.append(tok)
        pos = m.end()
    out.append(sql[pos:])
    return "".join(out)


_INSERT_OR = re.compile(
    r"^\s*INSERT\s+OR\s+(REPLACE|IGNORE|ABORT|FAIL|ROLLBACK)\s+INTO\s+([A-Za-z_][\w.]*)",
    re.IGNORECASE,
)


def _simple_insert_columns(sql: str) -> Optional[list[str]]:
    """从 `INSERT INTO t(a,b,c) VALUES(...)` 里抠出列名列表。

    只处理「显式列清单」这一种形态 —— 项目里所有 INSERT 都是这种。
    解析不出就返回 None，调用方退回 `DO NOTHING`（宁可少覆盖也不报错）。
    """
    m = re.search(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+[\w.\"]+\s*\(([^)]*)\)",
                  sql, re.IGNORECASE)
    if not m:
        return None
    cols = [c.strip().strip('"`[]') for c in m.group(1).split(",") if c.strip()]
    # 只接受「纯列名清单」。若抓到的是 VALUES(...) 里的表达式（含数字/引号/
    # 运算符），说明列清单没写，直接放弃 —— 否则会拼出不存在的列名。
    if not cols or not all(re.fullmatch(r"[A-Za-z_]\w*", c) for c in cols):
        return None
    return cols


def _strip_insert_or(sql: str) -> tuple[Optional[str], str]:
    """拆掉 `INSERT OR XXX` 前缀，返回 (xxx, `INSERT INTO t(...) ...` 剩余部分)。

    ⚠️ 返回的 `rest` 必须**包含** `INSERT INTO t` —— 只去掉 `OR XXX` 两个词。
    早期版本把 `INSERT OR XXX` 整段都吃掉，调用方拼回 `INSERT ` 时表名就没了，
    PG 报 `syntax error at or near "("`。
    """
    m = _INSERT_OR.match(sql)
    if not m:
        return None, sql
    kind = m.group(1).upper()
    table = m.group(2)
    rest = sql[m.end():].lstrip()
    return kind, f"INSERT INTO {table} {rest}"


# `BEGIN IMMEDIATE <scope>` —— scope 是**给 PG 分锁粒度用的尾随标识符**，
# SQLite 不认它（`BEGIN IMMEDIATE` 后只能跟事务结束）。
#
# 为什么由 sqlcompat 统一剥：两个后端都经过这里，剥一次就够。若只在
# sqlitedriver 里剥，PG 那条路径仍要自己解析 scope，两边逻辑会漂移。
# 剥出来的 scope 由 pgdriver 从**原始 SQL** 重新解析（它需要这个值）。
_BEGIN_SCOPE_TAIL = re.compile(
    r"^(\s*BEGIN\s+(?:IMMEDIATE|EXCLUSIVE))\s+[A-Za-z_][\w:.\-]*\s*$",
    re.IGNORECASE)


def strip_begin_scope(sql: str) -> str:
    """把 `BEGIN IMMEDIATE <scope>` 还原成 `BEGIN IMMEDIATE`（SQLite 用）。"""
    m = _BEGIN_SCOPE_TAIL.match(sql)
    return m.group(1) if m else sql


# ---------------------------------------------------------------- 连接对象

class Cursor:
    """aiosqlite 游标的最小兼容面。

    调用方只会用这几种：`fetchone()` / `fetchall()` / `rowcount` /
    `async with conn.execute(...) as cur` / 异步迭代。
    """

    __slots__ = ("_rows", "_i", "_rowcount", "_closed")

    def __init__(self, rows: list, rowcount: int = -1) -> None:
        self._rows = rows
        self._i = 0
        self._rowcount = rowcount
        self._closed = False

    async def fetchone(self):
        if self._i >= len(self._rows):
            return None
        row = self._rows[self._i]
        self._i += 1
        return row

    async def fetchall(self) -> list:
        rest = self._rows[self._i:]
        self._i = len(self._rows)
        return rest

    @property
    def rowcount(self) -> int:
        return self._rowcount

    async def close(self) -> None:
        self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        row = await self.fetchone()
        if row is None:
            raise StopAsyncIteration
        return row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


class _Awaitable:
    """既能 `await x` 又能 `async with x` 的返回值。

    aiosqlite 的 `conn.execute()` 不是协程，返回的就是这种对象，
    所以项目里两种写法并存。这里保持一致，调用方才不用改。
    """

    async def _resolve(self):
        raise NotImplementedError

    def __await__(self):
        return self._resolve().__await__()

    async def __aenter__(self):
        self._cur = await self._resolve()
        return self._cur

    async def __aexit__(self, *exc) -> None:
        # 与 aiosqlite 的 `async with conn.execute(...)` 一致：退出即关游标
        cur = getattr(self, "_cur", None)
        close = getattr(cur, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass


class Connection:
    """跨后端的连接对象。子类实现 `_execute_raw` 等原语。"""

    backend = BACKEND_SQLITE

    def __init__(self) -> None:
        self._row_factory = None

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:
        """调用方只会赋两种值：`sqlcompat.Row` 或 `None`。

        `sqlcompat.Row` 是 PG 后端用的行类；SQLite 后端必须换成**原生的**
        `sqlite3.Row` —— aiosqlite 的 row_factory 是两参数回调
        `(cursor, row) -> row`，签名与本类构造不同。换成原生 Row 后，
        SQLite 路径的行为连一个字节都不变（这正是这一层要保证的）。
        """
        if value is None:
            self._row_factory = None
        elif self.backend == BACKEND_SQLITE:
            self._row_factory = sqlite3.Row
        else:
            self._row_factory = value
        self._apply_row_factory(self._row_factory)

    def _apply_row_factory(self, value) -> None:
        """子类把 row_factory 真正装到驱动连接上。"""

    def _prepare(self, sql: str) -> str:
        """方言改写钩子。基类只做「两端通用」的剥离。

        ⚠️ 子类覆盖时必须先调 `super()._prepare(sql)` —— 否则
        `BEGIN IMMEDIATE <scope>` 的 scope 不会被剥掉，SQLite 会语法错。
        """
        return strip_begin_scope(sql)

    # ---- 子类实现 ----
    async def _execute_raw(self, sql: str, params: tuple) -> Cursor:
        raise NotImplementedError

    async def _executemany_raw(self, sql: str, seq: list) -> None:
        raise NotImplementedError

    async def _commit_raw(self) -> None:
        raise NotImplementedError

    async def _rollback_raw(self) -> None:
        raise NotImplementedError

    async def _close_raw(self) -> None:
        raise NotImplementedError

    # ---- 公共 API ----
    def execute(self, sql: str, params: Iterable | None = None) -> _Awaitable:
        args = tuple(params or ())

        class _Exec(_Awaitable):
            async def _resolve(_self) -> Cursor:
                return await self._execute_raw(sql, args)

        return _Exec()

    def executemany(self, sql: str, seq: Iterable) -> _Awaitable:
        rows = list(seq)

        class _Many(_Awaitable):
            async def _resolve(_self) -> Cursor:
                await self._executemany_raw(sql, rows)
                return Cursor([], rowcount=len(rows))

        return _Many()

    def executescript(self, sql: str) -> _Awaitable:
        class _Script(_Awaitable):
            async def _resolve(_self) -> Cursor:
                for stmt in split_statements(sql):
                    await self._execute_raw(stmt, ())
                return Cursor([])

        return _Script()

    async def commit(self) -> None:
        await self._commit_raw()

    async def rollback(self) -> None:
        await self._rollback_raw()

    async def close(self) -> None:
        await self._close_raw()

    def transaction(self):
        raise NotImplementedError

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


def split_statements(sql: str) -> list[str]:
    """按 `;` 拆分 DDL 脚本，跳过字符串字面量与注释里的分号。

    `executescript` 在项目里只用来跑建表语句（没有函数体 / 触发器 / 存储过程），
    所以「按顶层分号拆分」是安全的。仍然要扫字面量，否则 `DEFAULT ';'`、
    注释里的分号会把一条语句切成两半。

    ⚠️ 必须**逐字符**扫，不能先按 token 分段再切 —— 那样「注释前面的那一大段
    文本」会被整块塞进缓冲而不切分，导致多条 CREATE 合并成一条
    （2026-11-19 实测：team_store 的 12 条 DDL 被并成 8 条，
    aiosqlite 报 `You can only execute one statement at a time`）。
    """
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        # 单引号字符串：'' 是转义，整块跳过
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            i = j
            continue
        # 双引号标识符："" 是转义
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            i = j
            continue
        # 行注释：到行尾
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            j = n if j < 0 else j + 1
            buf.append(sql[i:j])
            i = j
            continue
        # 块注释：到 */
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            buf.append(sql[i:j])
            i = j
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def connect(path) -> Connection:
    """按配置选择后端，返回连接对象（用在 `async with` 里）。

    `path` 对 SQLite 是文件路径；若 `HLZ_DB_URL` 指向 postgres，
    `path` 被忽略 —— 这样 126 处调用点一个字都不用改。
    """
    from . import dbselect

    if dbselect.is_postgres():
        from .pgdriver import PgConnection
        return PgConnection(path)
    from .sqlitedriver import SqliteConnection
    return SqliteConnection(path)