"""SQLite 后端：**原样包装 aiosqlite**，零行为变化。

## 为什么不自己实现

这一层存在的唯一目的是「给 PG 后端一个同样的接口」，而不是「重新实现一个
SQLite 驱动」。当前生产库是 SQLite，1242 条测试是唯一可信的回归网 ——
只要这里还用 aiosqlite，SQLite 路径的行为就是**逐字节相同**的，
不存在「适配层引入了 bug 但测试没抓到」的风险。

方言改写对 SQLite 后端**全部关闭**（`_prepare` 原样返回）。

## `aiosqlite.connect()` 的两种用法都要支持

    async with aiosqlite.connect(path) as conn:     # 项目里 126 处都是这种
    conn = await aiosqlite.connect(path)            # app 内没有，测试可能有

所以 `connect()` 返回的对象既是 `async with` 上下文，也能被 `await`。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

import aiosqlite

from .sqlcompat import BACKEND_SQLITE, Connection, Cursor

# 让调用方可以 `except sqlcompat.IntegrityError` 跨后端捕获
IntegrityError = aiosqlite.IntegrityError


class SqliteConnection(Connection):
    backend = BACKEND_SQLITE

    def __init__(self, path) -> None:
        # ⚠️ 顺序要紧：基类 `__init__` 会写 `self.row_factory`，
        # 而 setter 会调 `_apply_row_factory`，需要 `_conn` 已存在。
        self._conn: Optional[aiosqlite.Connection] = None
        super().__init__()
        self.path = path

    async def _ensure(self) -> aiosqlite.Connection:
        if self._conn is None:
            # 调用方的父目录通常已由 init_db 建好；测试里用临时路径时
            # 目录可能还不存在，这里补一次 mkdir（幂等、无副作用）。
            try:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            except (OSError, TypeError):
                pass
            self._conn = await aiosqlite.connect(self.path)
            # 连接建好时如果已经设过 row_factory，补装上
            if self._row_factory is not None:
                self._conn.row_factory = self._row_factory
        return self._conn

    # ---- 原语 ----

    def _apply_row_factory(self, value) -> None:
        if self._conn is not None:
            self._conn.row_factory = value

    async def _execute_raw(self, sql: str, params: tuple) -> Cursor:
        conn = await self._ensure()
        # 基类只做「两端通用」的剥离：`BEGIN IMMEDIATE <scope>` 的 scope 是
        # 给 PG 分锁粒度用的，SQLite 不认（会语法错）。其余一律原样透传。
        return await conn.execute(self._prepare(sql), params)

    async def _executemany_raw(self, sql: str, seq: list) -> None:
        conn = await self._ensure()
        await conn.executemany(self._prepare(sql), seq)

    async def _commit_raw(self) -> None:
        conn = await self._ensure()
        await conn.commit()

    async def _rollback_raw(self) -> None:
        conn = await self._ensure()
        await conn.rollback()

    async def _close_raw(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await conn.close()

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                await conn._ensure()
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

    # `conn = await connect(path)` 这种写法（aiosqlite 支持，这里保持）
    def __await__(self):
        async def _ret():
            await self._ensure()
            return self
        return _ret().__await__()