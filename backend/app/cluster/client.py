"""进程级 Redis 客户端。

## 按 event loop 缓存（与 asyncpg 同一个坑）

`redis.asyncio.Redis` 的连接池绑定在创建它的事件循环上。项目里所有测试都是
`asyncio.run(...)`，每个用例一个新循环；如果客户端是模块级单例，第二个用例
拿着绑定在已关闭循环上的连接，就会 `RuntimeError: Event loop is closed`。

这个坑在 pgdriver 上已经踩过一次（425 条 TooManyConnectionsError），
所以这里从一开始就按 `id(loop)` 分桶。

## 没有配 Redis 时返回 None

调用方必须处理 None（降级到进程内实现）。不抛异常是刻意的：
本地开发、CI、现有 1242 条测试都不该被 Redis 拖累。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .config import redis_url

logger = logging.getLogger(__name__)

_clients: dict[int, Any] = {}
_locks: dict[int, asyncio.Lock] = {}


async def get_redis() -> Optional[Any]:
    """拿到本 event loop 的 Redis 客户端；没配 Redis 返回 None。"""
    url = redis_url()
    if not url:
        return None

    try:
        import redis.asyncio as aioredis
    except ImportError:  # pragma: no cover - 依赖缺失时给出清晰提示
        logger.error("HLZ_REDIS_URL 已设置但没装 redis 包：pip install redis")
        return None

    loop = asyncio.get_running_loop()
    key = id(loop)
    cli = _clients.get(key)
    if cli is not None:
        return cli

    _reap_dead_loops(loop)

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cli = _clients.get(key)
        if cli is not None:
            return cli
        cli = aioredis.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=32,
            socket_connect_timeout=3.0,
            socket_timeout=5.0,
            health_check_interval=30,
        )
        _clients[key] = cli
        logger.info("Redis 客户端已建立")
        return cli


def _reap_dead_loops(current_loop) -> None:
    """丢掉绑定在已关闭 loop 上的客户端。

    只从字典移除、不 await 关闭：旧 loop 已经关了，在**当前** loop 上
    await 旧客户端的 `aclose()` 会炸（同 pgdriver 的 `_reap_dead_pools`）。
    底层连接会随 TCP 超时被 Redis 服务端清掉。

    ⚠️ redis-py 的 `ConnectionPool.disconnect()` 在 asyncio 版是**协程**，
    不 await 会留下 `RuntimeWarning`。这里只关闭 in-use 标记并让 GC 收，
    不调 disconnect —— 在错误的 loop 上跑它本身也不安全。
    """
    for key in list(_clients.keys()):
        if key == id(current_loop):
            continue
        _clients.pop(key, None)
        _locks.pop(key, None)


async def close_redis() -> None:
    """关闭当前 loop 的客户端（应用 shutdown / 测试清理用）。"""
    try:
        key = id(asyncio.get_running_loop())
    except RuntimeError:
        return
    cli = _clients.pop(key, None)
    _locks.pop(key, None)
    if cli is None:
        return
    try:
        await cli.aclose()
    except Exception:  # noqa: BLE001
        pass


async def ping() -> bool:
    """探活。没配 Redis 返回 False（调用方据此降级）。"""
    cli = await get_redis()
    if cli is None:
        return False
    try:
        return bool(await cli.ping())
    except Exception:  # noqa: BLE001
        logger.warning("Redis 不可用", exc_info=True)
        return False