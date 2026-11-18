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

# 池参数。
#
# `min_size=0` 是刻意的：**空池不占 PG 连接**。测试里每个用例
# `asyncio.run()` 一个新 event loop，asyncpg 的池绑定 loop，于是每个用例都会
# 建一个新池。若 min_size=2，几百个用例就是几百 ×2 条连接，直接把 PG 的
# max_connections(100) 打爆（2026-11-19 实测 425 条
# `TooManyConnectionsError: sorry, too many clients already`）。
#
# 生产下 min_size=0 也合理：本项目的 DB 访问是「一连串短查询」，
# 首次建连的 35ms 只付一次，之后连接会留在池里直到被回收。
POOL_MIN = 0
POOL_MAX = 8
# 池空闲多久后关闭（秒）。测试里 loop 频繁更替，靠它兜住没被 prune 的池。
POOL_MAX_INACTIVE = 60.0
ACQUIRE_TIMEOUT = 30.0

_pools: dict[int, Any] = {}
_pool_locks: dict[int, asyncio.Lock] = {}

# 已经确保存在的 schema（每进程一次 CREATE SCHEMA IF NOT EXISTS）
_known_schemas: set[str] = set()

# 表 -> 主键列名（缓存）
_pk_cache: dict[str, list[str]] = {}

# 哪些 SQLite 语句在 PG 里是「空操作」
_NOOP_PRAGMA = re.compile(
    r"^\s*PRAGMA\s+(journal_mode|busy_timeout|foreign_keys|synchronous|"
    r"cache_size|temp_store|mmap_size|encoding|user_version)\b",
    re.IGNORECASE,
)

# `BEGIN IMMEDIATE` / `BEGIN EXCLUSIVE` 是 SQLite 的写锁语法，PG 只有 `BEGIN`。
#
# ## 不能简单降级成 `BEGIN`
#
# SQLite 的 `BEGIN IMMEDIATE` 立刻拿**数据库级写锁**，所以
#     读 MAX(event_seq) → +1 → 插入
# 这段在 SQLite 里是原子的。PG 默认的 `BEGIN` 不拿任何锁，两个并发事务都能
# 读到同一个 MAX，然后一个撞主键（2026-11-19 实测：
# `duplicate key value violates unique constraint "team_events_pkey"`）。
#
# ## 怎么还原
#
# 用一个事务级 advisory lock 顶替 SQLite 的库级写锁 —— 语义完全一致
# （都是「本事务独占写」），事务结束自动释放，不会泄漏。
# 代价是所有 `BEGIN IMMEDIATE` 事务串行；项目里只有 6 处、都是
# 「读-改-写-提交」的短事务（team 的停用/建队/发事件），可接受。
#
# 想再细一点可以按 team_id 做键，但那要在应用层传提示 —— 见下方
# `_LOCK_SCOPE_RE` + `conn.scope_lock()`：SQL 里写出 `BEGIN IMMEDIATE :key`
# 就可以按 key 分锁，不同队不再互相排队。

# 默认全局键（`BEGIN IMMEDIATE` 不带 key 时用）。保留它是为了向后兼容：
# 现有 6 处调用点不改也能跑，只是仍全局串行。
_WRITE_LOCK_KEY = 0x68_6C_7A_02   # 'hlz' + 版本号，固定值即可

# `BEGIN IMMEDIATE <scope>` —— scope 可以是任意标识符字面量。
#
# 为什么用「SQL 里带尾随标识符」而不是新 API：6 处调用点写的都是
# `conn.execute("BEGIN IMMEDIATE")` 这种字符串。加一个尾随 scope 是**纯增量**
# 改动（不改的那些照旧工作），而且读代码时立刻能看出「这个事务锁的是什么粒度」，
# 比 `conn.begin_locked(team_id)` 更显式。
_BEGIN_LOCKED = re.compile(
    r"^\s*BEGIN\s+(?:IMMEDIATE|EXCLUSIVE)(?:\s+([A-Za-z_][\w:.\-]*))?\s*$",
    re.IGNORECASE)
# 纯 `BEGIN` / `BEGIN DEFERRED` 不拿锁，只开事务
_BEGIN_PLAIN = re.compile(r"^\s*BEGIN(?:\s+DEFERRED)?\s*;?\s*$", re.IGNORECASE)


def lock_key_for(scope: str | None) -> int:
    """把 scope 字符串映射成 `pg_advisory_xact_lock` 的 64 位键。

    ## 为什么不用 Python 的 `hash()`
    `hash()` 在同一进程内稳定，但**跨进程/跨版本不稳定**（PYTHONHASHSEED
    随机化字符串哈希）。advisory lock 的键必须让所有实例算出同一个数，
    否则两个实例以为自己在锁不同的东西 —— 这比不锁更危险（看起来有保护）。

    所以用 `blake2b` 取 8 字节，截成有符号 64 位。

    ## 为什么 scope=None 仍用旧常量
    向后兼容：不带 scope 的 `BEGIN IMMEDIATE` 必须与之前行为完全一致
    （所有这种事务互斥）。这样升级不需要同时改 6 处业务代码。
    """
    if not scope:
        return _WRITE_LOCK_KEY
    import hashlib
    digest = hashlib.blake2b(scope.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big", signed=False)
    # pg_advisory_xact_lock 接受 bigint（有符号），折到正区间避免踩 int8 边界
    return value % (2 ** 63 - 1)

_TABLE_INFO = re.compile(r"^\s*PRAGMA\s+table_info\s*\(\s*['\"]?([\w.]+)['\"]?\s*\)",
                         re.IGNORECASE)

# SQLite 的 `INTEGER PRIMARY KEY AUTOINCREMENT` -> PG 的 IDENTITY
_AUTOINC = re.compile(
    r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.IGNORECASE)

# DDL 里的类型映射。
#
# ⚠️ `INTEGER` -> `BIGINT` 是**必须**的，不是可选优化：SQLite 的 INTEGER 是
# 64 位，而 PG 的 INTEGER 是 32 位。项目里所有时间戳都是毫秒
# （`int(time.time()*1000)` ≈ 1.7e12），塞进 int4 直接
# `value out of int32 range`。2026-11-19 实测就是这么炸的。
#
# 只在 CREATE TABLE / ALTER TABLE 上做，不碰 SELECT —— 避免误改
# `CAST(x AS INTEGER)` 这类表达式。
_DDL_TYPE_MAP = (
    (re.compile(r"\bINTEGER\b", re.IGNORECASE), "BIGINT"),
    (re.compile(r"\bREAL\b", re.IGNORECASE), "DOUBLE PRECISION"),
    (re.compile(r"\bBLOB\b", re.IGNORECASE), "BYTEA"),
)

_DDL_HEAD = re.compile(r"^\s*(CREATE\s+TABLE|ALTER\s+TABLE|CREATE\s+INDEX|"
                       r"CREATE\s+UNIQUE\s+INDEX)", re.IGNORECASE)

_LEADING_COMMENT = re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/\s*)+", re.DOTALL)


def _strip_leading_comments(sql: str) -> str:
    """剥掉语句**开头**的注释和空行。

    `split_statements` 按分号切分，切出来的下一句会带着它前面的注释
    （`-- 名字钉住：...`）。若不剥掉，`_DDL_HEAD.match()` 就匹配不到
    `CREATE TABLE`，类型映射整段失效 —— 2026-11-19 实测：`team_name_pins`
    的 `pinned_at` 建成了 `integer` 而不是 `bigint`，插入毫秒时间戳就
    `value out of int32 range`。注释里的 SQL 关键字不是语句本身，必须忽略。
    """
    return _LEADING_COMMENT.sub("", sql, count=1)

# `ALTER TABLE t RENAME TO old` 在 PG 里语法相同，保留。
# `IF NOT EXISTS` 用于索引/表都可，PG 支持。

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

    # 上一个 loop 的池已经没人用了（loop 都换了），关掉它再建新的。
    # 不关的话连接会一直挂在 PG 上直到进程退出 —— 测试里就是几百条。
    _reap_dead_pools(loop)

    lock = _pool_locks.setdefault(key, asyncio.Lock())
    async with lock:
        pool = _pools.get(key)
        if pool is not None and not pool._closed:
            return pool
        pool = await asyncpg.create_pool(
            dbselect.pg_dsn(),
            min_size=POOL_MIN,
            max_size=POOL_MAX,
            max_inactive_connection_lifetime=POOL_MAX_INACTIVE,
            command_timeout=None,
            init=_init_conn,
        )
        _pools[key] = pool
        logger.debug("PG 连接池已建立（min=%d max=%d）", POOL_MIN, POOL_MAX)
        return pool


def _reap_dead_pools(current_loop) -> None:
    """关掉属于「已经不在跑」的 loop 的池。

    asyncpg 的池强绑定 loop，且 `pool.close()` 必须在那个 loop 上跑。
    旧 loop 已经关了，所以在**当前** loop 上只能直接释放底层连接
    （`Pool._holders` 里的 `_con`），不能 await pool.close()。
    """
    for key in list(_pools.keys()):
        pool = _pools.get(key)
        if pool is None or pool._closed:
            _pools.pop(key, None)
            _pool_locks.pop(key, None)
            continue
        loop = getattr(pool, "_loop", None)
        if loop is current_loop or (loop is not None and not loop.is_closed()):
            continue
        # 旧 loop 已关：把底层连接直接丢掉（它们对 PG 来说已是僵尸）
        try:
            for holder in list(getattr(pool, "_holders", [])):
                con = getattr(holder, "_con", None)
                if con is not None:
                    try:
                        con.terminate()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        _pools.pop(key, None)
        _pool_locks.pop(key, None)


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


async def warm_pool(n: int = 2) -> None:
    """预热连接池：应用启动时调一次，避免「首个请求承担建连成本」。

    ## 为什么需要

    实测：PG 冷建连 ~200ms（TCP + 认证握手），之后 1.5ms。
    池是懒建的，第一个用到数据库的请求会等这 200ms。

    对**记忆预取**这类有严格时序的功能，200ms 是致命的：预取必须在
    首轮模型调用（约 50ms~几秒）内 settle，否则这一轮就不注入。
    SQLite 建连是 0.17ms，所以这个成本在 SQLite 时代根本不存在 ——
    2026-11-19 实测：PG 下 `test_loop_consumes_only_after_tool_steps`
    因为选择器要等 200ms 建连而失败。

    预热后第一个请求直接命中池。`min_size=0` 的池允许「先建再还」，
    归还的连接会留在池里（`max_inactive_connection_lifetime=60s`）。
    """
    if asyncpg is None:
        return
    pool = await _get_pool()
    conns = []
    try:
        for _ in range(max(1, n)):
            conns.append(await asyncio.wait_for(pool.acquire(), ACQUIRE_TIMEOUT))
    finally:
        for c in conns:
            try:
                await pool.release(c)
            except Exception:  # noqa: BLE001
                pass


async def primary_key(table: str, schema: str = "public") -> list[str]:
    """查表的主键列（带缓存）。查不到返回空列表。

    ⚠️ `schema` 必须显式传，不能用 `current_schema()`：这里走的是**池连接**，
    不是 `PgConnection` 那条，它的 search_path 是上一个借用者留下的
    （测试里每个用例一个 schema），用 current_schema() 会查到别人的表，
    然后拿到错的主键列名去写 ON CONFLICT，报「没有唯一约束匹配」。
    """
    name = table.split(".")[-1].strip('"').lower()
    key = f"{schema}.{name}"
    if key in _pk_cache:
        return _pk_cache[key]
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT a.attname "
        "FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
        "WHERE c.relname = $1 AND i.indisprimary AND n.nspname = $2 "
        "ORDER BY array_position(i.indkey, a.attnum)",
        name, schema,
    )
    cols = [r[0] for r in rows]
    if not cols:
        # 不是主键但可能有唯一约束 —— ON CONFLICT 只能指定唯一索引列
        rows = await pool.fetch(
            "SELECT a.attname "
            "FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
            "WHERE c.relname = $1 AND i.indisunique AND n.nspname = $2 "
            "ORDER BY i.indexrelid LIMIT 1",
            name, schema,
        )
        cols = [r[0] for r in rows]
    _pk_cache[key] = cols
    return cols


def forget_schema_cache() -> None:
    """建表/改表之后调用（PRAGMA 元数据缓存失效）。"""
    _pk_cache.clear()


class PgConnection(Connection):
    backend = BACKEND_POSTGRES

    def __init__(self, path=None) -> None:
        super().__init__()
        self._raw = None
        self._in_tx = False
        self._dirty = False        # 是否有未提交改动
        self._sp = 0
        # 测试隔离用：同一路径 -> 同一 schema（生产恒为 public）
        self.schema = dbselect.pg_schema_for(path)

    # ---------------- 连接生命周期 ----------------

    async def _ensure(self):
        if self._raw is None:
            pool = await _get_pool()
            self._raw = await asyncio.wait_for(pool.acquire(), ACQUIRE_TIMEOUT)
            await self._use_schema(self._raw)
        return self._raw

    async def _use_schema(self, raw) -> None:
        """把会话的 search_path 指到本连接对应的 schema。

        `public` 也要显式设 —— 池里借出的连接可能带着上一个用例的 search_path。
        """
        schema = self.schema
        if schema not in _known_schemas:
            await raw.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            _known_schemas.add(schema)
        await raw.execute(f'SET search_path TO "{schema}", public')

    async def _begin(self, *, lock: bool = False, scope: str | None = None) -> None:
        raw = await self._ensure()
        await raw.execute("BEGIN")
        self._in_tx = True
        if lock:
            # 顶替 SQLite 的库级写锁：整个事务独占写，事务结束自动释放。
            # `scope` 决定锁粒度（None = 全局，与迁移前行为一致）。
            await raw.execute("SELECT pg_advisory_xact_lock($1)",
                              lock_key_for(scope))
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
        # 基类先剥掉两端通用的部分（`BEGIN IMMEDIATE <scope>` 的尾随标识符）。
        # `scope` 由 `_execute_raw` 从**原始 SQL** 里解析 —— 那时还没被剥。
        from .sqlcompat import strip_begin_scope
        raw = strip_begin_scope(sql.strip())

        # 剥掉开头的注释行：`_DDL_HEAD` 之类的前缀判断必须看**语句**，
        # 不能看注释里的文字（注释里写了 "CREATE TABLE" 会误判）。
        raw = _strip_leading_comments(raw)

        # DDL 方言：AUTOINCREMENT -> IDENTITY（`a2a_audit.id` 是唯一一处）
        if "AUTOINCREMENT" in raw.upper():
            raw = _AUTOINC.sub("BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", raw)

        # DDL 类型映射：SQLite 的 INTEGER 是 64 位，PG 的 INT4 是 32 位。
        # 时间戳是毫秒（1.7e12），不映射就到不了 1 纳秒 —— 直接 DataError。
        if _DDL_HEAD.match(raw):
            for pat, repl in _DDL_TYPE_MAP:
                raw = pat.sub(repl, raw)

        # 1) PG 里无意义的 PRAGMA -> 空操作（返回空结果）
        if _NOOP_PRAGMA.match(raw):
            return ""

        # 2) PRAGMA table_info(t) -> information_schema，还原成 SQLite 的 6 列形状
        #
        # ⚠️ 必须按 `table_schema` 过滤！`information_schema.columns` 是跨 schema 的，
        # 只按 table_name 过滤会把别的 schema 里同名表的列也捞出来，
        # 结果就是「user_id 已存在」的假象 → 跳过 ALTER → 后续 UndefinedColumnError。
        # 测试里每个用例一个 schema，这个 bug 会 100% 命中（实测 57 条报错）。
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
                "  JOIN pg_namespace n ON n.oid = cl.relnamespace "
                "  JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attnum = ANY(i.indkey) "
                f"  WHERE cl.relname = '{table}' AND i.indisprimary "
                "    AND n.nspname = current_schema()"
                ") pk ON pk.attname = c.column_name "
                f"WHERE c.table_name = '{table}' "
                "  AND c.table_schema = current_schema() "
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

        # 5) sqlite_master -> 等价的系统视图
        #
        # 项目里只有一种用法：`SELECT 1 FROM sqlite_master WHERE type='table'
        # AND name='xxx'`（探测表是否存在）。PG 对应 pg_class + pg_namespace。
        if re.search(r"\bsqlite_master\b", raw, re.IGNORECASE):
            raw = re.sub(
                r"\bsqlite_master\b",
                "(SELECT CASE WHEN c.relkind IN ('r','p') THEN 'table' "
                " WHEN c.relkind = 'i' THEN 'index' ELSE 'other' END AS type, "
                " c.relname AS name, NULL AS tbl_name, NULL AS sql "
                " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                " WHERE n.nspname = current_schema()) AS sqlite_master",
                raw, flags=re.IGNORECASE)

        return raw

    async def _to_on_conflict(self, kind: str, rest: str) -> str:
        """把 `INSERT OR REPLACE|IGNORE INTO t(...)` 改写成 ON CONFLICT 形式。

        `rest` 已经是完整的 `INSERT INTO t(...) VALUES(...)`（`_strip_insert_or`
        负责把 `OR XXX` 换成 `INSERT INTO`）。
        """
        sql = rest
        # 已经带 ON CONFLICT 就不重复加
        if re.search(r"\bON\s+CONFLICT\b", sql, re.IGNORECASE):
            return sql

        m = re.search(r"\bINTO\s+([A-Za-z_][\w.]*)", sql, re.IGNORECASE)
        if not m:
            return sql
        table = m.group(1)

        cols = _simple_insert_columns(sql)
        pks = await primary_key(table, self.schema)

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

    @staticmethod
    def _sanitize_params(params: tuple) -> tuple:
        """去掉字符串参数里的 NUL 字节。

        PostgreSQL 的 text/varchar **在协议层就不允许 `0x00`**
        （`invalid byte sequence for encoding "UTF8": 0x00`），SQLite 则原样存。
        这是无法跨越的表示能力差异，不是配置问题。

        项目里唯一会带进 NUL 的地方是搜索：用户把乱码粘进搜索框
        （`test_nasty_query_never_raises` 就是照这个场景写的）。在那里报错比
        返回空结果糟糕得多 —— 搜索框不该因为粘贴了控制字符就 500。

        所以在这一层削掉 NUL，让「查不到」而不是「崩掉」。写入路径同理：
        含 NUL 的文本在 PG 里根本存不进去，削掉比报错更接近用户预期。
        """
        if not params:
            return params
        out = []
        for p in params:
            if isinstance(p, str) and "\x00" in p:
                p = p.replace("\x00", "")
            out.append(p)
        return tuple(out)

    async def _execute_raw(self, sql: str, params: tuple) -> Cursor:
        # `BEGIN IMMEDIATE` 走特殊路径：要把适配层的 `_in_tx` 标记同步过来，
        # 否则后续 commit/rollback 会漏掉这个事务；并且必须真拿写锁，
        # 否则 `MAX(seq)+1` 这类「读后写」在并发下会撞主键。
        if _BEGIN_LOCKED.match(sql.strip()):
            m_lock = _BEGIN_LOCKED.match(sql.strip())
            scope = m_lock.group(1) if m_lock else None
            await self._ensure()
            if not self._in_tx:
                await self._begin(lock=True, scope=scope)
            return Cursor([], rowcount=-1)

        if _BEGIN_PLAIN.match(sql.strip()):
            await self._ensure()
            if not self._in_tx:
                await self._begin()
            return Cursor([], rowcount=-1)

        stmt = await self._prepare(sql)
        if not stmt:
            return Cursor([], rowcount=-1)
        await self._ensure()
        if not self._in_tx:
            await self._begin()
        params = self._sanitize_params(params)

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
        rows = [self._sanitize_params(tuple(r)) for r in seq]
        try:
            await self._raw.executemany(stmt, rows)
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
        """跑一段 DDL 脚本，**自带事务**（与 sqlite3 的 executescript 语义一致）。

        sqlite3 的 `executescript` 会先 COMMIT 掉挂起的事务，再整段执行。
        这里照抄这个语义，有两个实际好处：

        1. 与 SQLite 行为对齐 —— 调用方不用为两个后端写两套。
        2. **让 `try: ALTER TABLE ... except: pass` 能继续跑**：脚本提交后
           `_in_tx` 为假，下一条语句会重新 BEGIN + 建 SAVEPOINT，
           失败只回滚到那个新 SAVEPOINT，不会把刚建好的表一起吞掉
           （2026-11-19 实测：不提交时 `relation "t" does not exist`）。
        """
        from .sqlcompat import split_statements

        stmts = split_statements(sql)
        if not stmts:
            return Cursor([])

        await self._ensure()
        # 挂起的事务先提交（同 sqlite3）
        if self._in_tx:
            await self._commit_raw()

        await self._begin()
        try:
            for stmt in stmts:
                await self._execute_raw(stmt, ())
            await self._commit_raw()
        except Exception:
            await self._rollback_raw()
            raise
        finally:
            # 表结构变了，主键缓存必须失效
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