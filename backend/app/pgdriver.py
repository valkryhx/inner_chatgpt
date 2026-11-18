"""PostgreSQL 后端：用 asyncpg 实现 `sqlcompat.Connection` 接口。

## 三个必须解决的语义差异

### 1. 占位符
项目里 126 处全用 `?`。asyncpg 只认 `$1..$n`。改写时**必须跳过字符串
字面量**（搜索功能里有 `LIKE ? ESCAPE '\\'`，还有用户输入里的问号）。

### 2. `INSERT OR REPLACE` / `INSERT OR IGNORE`
SQLite 专有。PG 用 `ON CONFLICT`，但**需要知道主键**才能写 `DO UPDATE`。
主键从 `information_schema` 查一次并缓存（一个进程内表结构不变）。

### 3. 失败语句不能污染事务
SQLite 里一条语句失败不影响后续语句（`try: ALTER TABLE ... except: pass`
是项目里 init_db 的标准迁移写法）。PG 里语句一失败，整个事务进入 aborted
状态，后续所有语句都报 `current transaction is aborted`。

解法：事务开起来后立刻建一个 SAVEPOINT；**只有出错时**才
`ROLLBACK TO SAVEPOINT` 并重建。正常路径不付这个代价。

## 连接池

必须用池。实测：无池 `connect+query+close` 35.8ms，有池 1.5ms。
35ms 全是 TCP 建连 + 认证握手。

asyncpg 的池**绑定事件循环**，测试里每个用例 `asyncio.run` 会换新循环，
所以池按循环分别缓存（`_pools` 以 `id(loop)` 为键）。

## 事务生命周期

`async with connect(path) as conn:` 从池里借一条连接，退出时归还：
- 已 commit → 直接归还
- 未 commit → 回滚后归还（与 aiosqlite `__aexit__` 行为一致）
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Iterable, Optional

from . import dbselect
from .sqlcompat import (
    BACKEND_POSTGRES,
    Connection,
    Cursor,
    Row,
    _rewrite_placeholders,
    _simple_insert_columns,
    _strip_insert_or,
    IntegrityError,
)

logger = logging.getLogger(__name__)

# 池参数。`max_size` 要够：一个请求内可能串用多条连接（例如 team 建队时
# 持有连接又调用辅助函数），给 20 留足余量。
POOL_MIN = 2
POOL_MAX = 20
ACQUIRE_TIMEOUT = 30.0

_pools: dict[int, Any] = {}
_pool_locks: dict[int, asyncio.Lock] = {}

# 表 -> 主键列名（缓存）
_pk_cache: dict[str, list[str]] = {}

# 哪些 SQLite 语句在 PG 里是「空操作」
_NOOP_PRAGMA = re.compile(
    r"^\s*PRAGMA\s+(journal_mode|busy_timeout|foreign_keys|synchronous|"
    r"cache_size|temp_store|mmap_size|encoding|user_version)\b",
    re.IGNORECASE,
)

_TABLE_INFO = re.compile(r"^\s*PRAGMA\s+table_info\s*\(\s*['\"]?([\w.]+)['\"]?\s*\)",
                         re.IGNORECASE)

# 首关键字分类
_SELECT_HEADS = ("SELECT", "WITH", "VALUES", "TABLE", "SHOW", "EXPLAIN")
_WRITE_HEADS = ("INSERT", "UPDATE", "DELETE", "MERGE")

_ROWCOUNT = re.compile(r"^(?:INSERT|UPDATE|DELETE|MERGE)\s+(?:\d+\s+)?(\d+)", re.IGNORECASE)

# PG 里哪些异常应当被当作「约束违规」——与 sqlite3.IntegrityError 对齐
try:  # asyncpg 缺失时（只跑 SQLite 的部署）不要炸
    import asyncpg  # noqa: F401
    _INTEGRITY_TYPES: tuple = (
        asyncpg.UniqueViolationError,
        asyncpg.ForeignKeyViolationError,
        asyncpg.NotNullViolationError,
        asyncpg.CheckViolationError,
        asyncpg.ExclusionViolationError,
    )
except Exception:  # noqa: BLE001
    asyncpg = None  # type: ignore
    _INTEGRITY_TYPES = ()


def _wrap_error(exc: BaseException) -> BaseException:
    """把 PG 的约束违规映射成 `sqlcompat.IntegrityError`。"""
    if _INTEGRITY_TYPES and isinstance(exc, _INTEGRITY_TYPES):
        err = IntegrityError(str(exc))
        err.__cause__ = exc
        return err
    return exc


async def _get_pool():
    if asyncpg is None:
        raise RuntimeError(
            "HLZ_DB_URL 指向 PostgreSQL，但没装 asyncpg。"
            "请 pip install asyncpg"
        )
    loop = asyncio.get_running_loop()
    key = id(loop)
    pool = _pools.get(key)
    if pool is not None and not pool._closed:
        return pool
    lock = _pool_locks.setdefault(key, asyncio.Lock())
    async with lock:
        pool = _pools.get(key)
        if pool is not None and not pool._closed:
            return pool
        pool = await asyncpg.create_pool(
            dbselect.pg_dsn(),
            min_size=POOL_MIN,
            max_size=POOL_MAX,
            command_timeout=None,
            init=_init_conn,
        )
        _pools[key] = pool
        logger.info("PG 连接池已建立（min=%d max=%d）", POOL_MIN, POOL_MAX)
        return pool


async def _init_conn(conn) -> None:
    """每条新建连接做一次：让 TEXT 与 Python str 的往返保持原样。"""
    # 关闭 JIT，小查询下单条能省 1-3ms
    try:
        await conn.execute("SET jit = off")
    except Exception:  # noqa: BLE001
        pass


async def close_pool() -> None:
    """关闭当前循环的池（应用 shutdown / 测试清理用）。"""
    try:
        key = id(asyncio.get_running_loop())
    except RuntimeError:
        return
    pool = _pools.pop(key, None)
    _pool_locks.pop(key, None)
    if pool is not None:
        await pool.close()


async def primary_key(table: str) -> list[str]:
    """查表的主键列（带缓存）。查不到返回空列表。"""
    name = table.split(".")[-1].strip('"').lower()
    if name in _pk_cache:
        return _pk_cache[name]
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT a.attname "
        "FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
        "WHERE c.relname = $1 AND i.indisprimary "
        "ORDER BY array_position(i.indkey, a.attnum)",
        name,
    )
    cols = [r[0] for r in rows]
    if not cols:
        # 不是主键但可能有唯一约束 —— ON CONFLICT 只能指定唯一索引列
        rows = await pool.fetch(
            "SELECT a.attname "
            "FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
            "WHERE c.relname = $1 AND i.indisunique "
            "ORDER BY i.indexrelid LIMIT 1",
            name,
        )
        cols = [r[0] for r in rows]
    _pk_cache[name] = cols
    return cols


def forget_schema_cache() -> None:
    """建表/改表之后调用（PRAGMA 元数据缓存失效）。"""
    _pk_cache.clear()


class PgConnection(Connection):
    backend = BACKEND_POSTGRES

    def __init__(self) -> None:
        super().__init__()
        self._raw = None
        self._in_tx = False
        self._dirty = False        # 是否有未提交改动
        self._sp = 0

    # ---------------- 连接生命周期 ----------------

    async def _ensure(self):
        if self._raw is None:
            pool = await _get_pool()
            self._raw = await asyncio.wait_for(pool.acquire(), ACQUIRE_TIMEOUT)
        return self._raw

    async def _begin(self) -> None:
        raw = await self._ensure()
        await raw.execute("BEGIN")
        self._in_tx = True
        await self._savepoint()

    async def _savepoint(self) -> None:
        self._sp += 1
        await self._raw.execute(f"SAVEPOINT hlz_sp_{self._sp}")

    async def _recover(self) -> None:
        """语句失败后把事务从 aborted 状态救回来。

        只有出错才走这条路 —— 正常路径不付代价。
        """
        try:
            await self._raw.execute(f"ROLLBACK TO SAVEPOINT hlz_sp_{self._sp}")
            await self._raw.execute(f"RELEASE SAVEPOINT hlz_sp_{self._sp}")
            await self._savepoint()
        except Exception:  # noqa: BLE001 - 救不回来就整体回滚
            logger.warning("PG savepoint 恢复失败，整事务回滚", exc_info=True)
            await self._rollback_raw()

    # ---------------- SQL 方言改写 ----------------

    async def _prepare(self, sql: str) -> str:
        raw = sql.strip()

        # 1) PG 里无意义的 PRAGMA -> 空操作（返回空结果）
        if _NOOP_PRAGMA.match(raw):
            return ""

        # 2) PRAGMA table_info(t) -> information_schema，还原成 SQLite 的 6 列形状
        m = _TABLE_INFO.match(raw)
        if m:
            table = m.group(1).split(".")[-1].strip('"').lower()
            return (
                "SELECT c.ordinal_position - 1 AS cid, c.column_name AS name, "
                "       c.data_type AS type, "
                "       CASE WHEN c.is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull, "
                "       c.column_default AS dflt_value, "
                "       CASE WHEN pk.attnum IS NOT NULL THEN 1 ELSE 0 END AS pk "
                "FROM information_schema.columns c "
                "LEFT JOIN ("
                "  SELECT a.attnum, a.attname FROM pg_index i "
                "  JOIN pg_class cl ON cl.oid = i.indrelid "
                "  JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attnum = ANY(i.indkey) "
                f"  WHERE cl.relname = '{table}' AND i.indisprimary"
                ") pk ON pk.attname = c.column_name "
                f"WHERE c.table_name = '{table}' "
                "ORDER BY c.ordinal_position"
            )

        # 已经是 $n 风格的 SQL 不再改写（手工迁移脚本可能直接写 $1）
        if "$1" not in raw:
            raw = _rewrite_placeholders(raw)

        # 3) INSERT OR REPLACE / IGNORE -> ON CONFLICT
        kind, rest = _strip_insert_or(raw)
        if kind is not None:
            raw = await self._to_on_conflict(kind, rest)

        # 4) rowid -> ctid（SQLite 的物理行序在 PG 里对应 ctid）
        if re.search(r"\browid\b", raw, re.IGNORECASE):
            raw = re.sub(r"\b([A-Za-z_][\w]*\.)?rowid\b", r"\1ctid", raw,
                         flags=re.IGNORECASE)

        return raw

    async def _to_on_conflict(self, kind: str, rest: str) -> str:
        """把 `INSERT OR REPLACE|IGNORE INTO t(...)` 改写成 ON CONFLICT 形式。

        `rest` 是去掉 `INSERT OR XXX ` 之后的 `INTO t(...) VALUES(...)`。
        """
        sql = "INSERT " + rest
        # 已经带 ON CONFLICT 就不重复加
        if re.search(r"\bON\s+CONFLICT\b", sql, re.IGNORECASE):
            return sql

        m = re.search(r"\bINTO\s+([A-Za-z_][\w.]*)", sql, re.IGNORECASE)
        if not m:
            return sql
        table = m.group(1)

        cols = _simple_insert_columns(sql)
        pks = await primary_key(table)

        if kind == "IGNORE":
            if pks:
                return f"{sql} ON CONFLICT ({', '.join(pks)}) DO NOTHING"
            return f"{sql} ON CONFLICT DO NOTHING"

        # REPLACE：冲突时覆盖所有非主键列
        if not cols or not pks:
            logger.warning("INSERT OR REPLACE 到 %s 时拿不到列/主键，退回 DO NOTHING", table)
            return f"{sql} ON CONFLICT DO NOTHING"
        pk_set = {p.lower() for p in pks}
        updates = [c for c in cols if c.lower() not in pk_set]
        if not updates:
            return f"{sql} ON CONFLICT ({', '.join(pks)}) DO NOTHING"
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in updates)
        return f"{sql} ON CONFLICT ({', '.join(pks)}) DO UPDATE SET {sets}"

    # ---------------- 原语 ----------------

    async def _execute_raw(self, sql: str, params: tuple) -> Cursor:
        stmt = await self._prepare(sql)
        if not stmt:
            return Cursor([], rowcount=-1)
        await self._ensure()
        if not self._in_tx:
            await self._begin()

        head = stmt.lstrip().split(None, 1)[0].upper() if stmt.strip() else ""
        try:
            if head in _SELECT_HEADS:
                rows = await self._raw.fetch(stmt, *params)
                return Cursor([Row(list(r.keys()), tuple(r.values())) for r in rows],
                              rowcount=len(rows))
            status = await self._raw.execute(stmt, *params)
            self._dirty = True
            m = _ROWCOUNT.match(status or "")
            return Cursor([], rowcount=int(m.group(1)) if m else -1)
        except Exception as exc:  # noqa: BLE001
            await self._recover()
            raise _wrap_error(exc) from None

    async def _executemany_raw(self, sql: str, seq: list) -> None:
        if not seq:
            return
        stmt = await self._prepare(sql)
        if not stmt:
            return
        await self._ensure()
        if not self._in_tx:
            await self._begin()
        try:
            await self._raw.executemany(stmt, [tuple(r) for r in seq])
            self._dirty = True
        except Exception as exc:  # noqa: BLE001
            await self._recover()
            raise _wrap_error(exc) from None

    async def _commit_raw(self) -> None:
        if self._in_tx:
            try:
                await self._raw.execute("COMMIT")
            finally:
                self._in_tx = False
                self._dirty = False

    async def _rollback_raw(self) -> None:
        if self._in_tx:
            try:
                await self._raw.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - 已经断了就算了
                pass
            finally:
                self._in_tx = False
                self._dirty = False

    async def _close_raw(self) -> None:
        raw, self._raw = self._raw, None
        if raw is None:
            return
        if self._in_tx:
            # 与 aiosqlite `__aexit__` 一致：没提交就回滚
            try:
                await raw.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            self._in_tx = False
        try:
            pool = await _get_pool()
            await pool.release(raw)
        except Exception:  # noqa: BLE001
            try:
                await raw.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------------- 扩展 API ----------------

    async def executescript(self, sql: str) -> Cursor:
        from .sqlcompat import split_statements
        await self._ensure()
        # DDL 全部放进一个事务：init 时半路失败要整体回滚，不能留半个 schema
        if not self._in_tx:
            await self._begin()
        for stmt in split_statements(sql):
            await self._execute_raw(stmt, ())
        # PG 的 CREATE TABLE ... 之后主键缓存要失效
        forget_schema_cache()
        return Cursor([])

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                await conn._ensure()
                if not conn._in_tx:
                    await conn._begin()
                return conn

            async def __aexit__(self, exc_type, exc, tb):
                if exc_type is None:
                    await conn._commit_raw()
                else:
                    await conn._rollback_raw()
                return False

        return _Tx()

    async def __aenter__(self):
        await self._ensure()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()