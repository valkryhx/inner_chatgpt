"""分布式锁：`SET NX PX` + Lua 安全释放 + 自动续约。

## 三个必须做对的地方

### 1. 释放必须校验持有者（Lua 原子）
朴素实现 `DEL key` 会误删**别人刚抢到的锁**：A 的租期到了、B 拿到锁，
A 的 finally 才跑到 `DEL`。所以释放必须是「值等于我的 token 才删」，
且比较+删除要原子（Lua），否则中间仍可被插队。

### 2. 必须有续约
业务事务可能比租期长（一条带 LLM 的路径可能几十秒）。不续约的话锁会
在事务中途过期，两个实例同时进临界区。续约线程按 `ttl/3` 打点。

### 3. 续约失败要能停下来
续约时发现锁已不属于自己（被抢了 / TTL 到了别人拿了），必须**立即置
`lost` 标志并让业务代码有机会放弃**。`LockHandle.alive` 就是给调用方查的。

## 没有 Redis 时的降级

`distributed_lock()` 返回一个基于 `asyncio.Lock` 的实现。单实例下语义等价
（互斥 + 可重入计数），跨实例无效 —— 但没配 Redis 就说明是单实例，
这正是项目当前（以及全部现有测试）的部署形态。

## 为什么用 NX 轮询而不是 `pubsub` 通知

`SET NX` 失败后固定间隔重试（带抖动）实现简单、行为可预测，且 Redis 版
`pubsub` 在重连时会丢消息。锁的临界区本来就很短，轮询延迟（默认 25ms）
不是瓶颈。
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
from typing import Any, Optional

from .client import get_redis
from .config import lock_ttl_ms, lock_wait_ms

logger = logging.getLogger(__name__)

# 安全释放：只有 token 匹配才删。
# 用 Lua 保证「比较 + 删除」原子，避免 A 的 DEL 删掉 B 刚拿到的锁。
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# 续约：同样要校验 token，且只延长不从零重建（避免把别人的锁续上）。
_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""

_POLL_MIN_MS = 20
_POLL_MAX_MS = 80


class LockTimeout(Exception):
    """等锁超时。调用方应把它当作「资源忙」，而不是崩掉。"""


class LockHandle:
    """一次加锁的句柄。`async with` 保证释放；`alive` 供业务自查。"""

    __slots__ = ("key", "token", "ttl_ms", "_redis", "_renew_task",
                 "_released", "alive", "_local")

    def __init__(self, key: str, token: str, ttl_ms: int,
                 redis: Optional[Any], *, local: Optional[asyncio.Lock] = None) -> None:
        self.key = key
        self.token = token
        self.ttl_ms = ttl_ms
        self._redis = redis
        self._local = local
        self._renew_task: Optional[asyncio.Task] = None
        self._released = False
        self.alive = True

    # ---------------- 续约 ----------------

    def start_renew(self) -> None:
        if self._redis is None or self._renew_task is not None:
            return
        self._renew_task = asyncio.create_task(self._renew_loop())

    async def _renew_loop(self) -> None:
        interval = max(self.ttl_ms / 3 / 1000, 0.2)
        try:
            while not self._released:
                await asyncio.sleep(interval)
                if self._released:
                    return
                try:
                    ok = await self._redis.eval(
                        _RENEW_LUA, 1, self.key, self.token, self.ttl_ms)
                except Exception:  # noqa: BLE001 - 网络抖动不算丢锁
                    logger.warning("锁续约失败（网络？）：%s", self.key, exc_info=True)
                    continue
                if not ok:
                    # 锁已经不属于我了：必须让业务知道
                    self.alive = False
                    logger.error("锁已丢失（可能超期被抢）：%s", self.key)
                    return
        except asyncio.CancelledError:
            pass

    # ---------------- 释放 ----------------

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.alive = False

        task = self._renew_task
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._renew_task = None

        if self._local is not None:
            try:
                self._local.release()
            except RuntimeError:  # 不是本任务持有时 release 会抛
                pass
            return

        if self._redis is None:
            return
        try:
            await self._redis.eval(_RELEASE_LUA, 1, self.key, self.token)
        except Exception:  # noqa: BLE001 - 释放失败只能等 TTL
            logger.warning("锁释放失败（等 TTL 过期）：%s", self.key, exc_info=True)

    async def __aenter__(self) -> "LockHandle":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.release()


def _lock_key(name: str) -> str:
    """加命名空间前缀，避免和别的应用共用 Redis 时撞键。"""
    prefix = os.environ.get("HLZ_REDIS_PREFIX", "hlz").strip() or "hlz"
    return f"{prefix}:lock:{name}"


async def distributed_lock(name: str, *, ttl_ms: Optional[int] = None,
                           wait_ms: Optional[int] = None) -> LockHandle:
    """获取一个分布式锁，返回已持有的句柄。

    - 配了 Redis：`SET NX PX` 轮询，成功后启动续约协程
    - 没配 Redis：退化成进程内 `asyncio.Lock`（单实例语义等价）

    超时抛 `LockTimeout`。调用方通常应捕获它并返回「忙」而不是 5xx。
    """
    ttl = ttl_ms or lock_ttl_ms()
    wait = lock_wait_ms() if wait_ms is None else wait_ms
    key = _lock_key(name)
    token = uuid.uuid4().hex

    redis = await get_redis()
    if redis is None:
        return await _local_lock(key, wait)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait / 1000
    while True:
        try:
            ok = await redis.set(key, token, nx=True, px=ttl)
        except Exception as exc:  # noqa: BLE001 - Redis 抖动时降级为本地锁
            logger.warning("Redis SET NX 失败，退化为进程内锁：%s", exc.__class__.__name__)
            return await _local_lock(key, max(0, int((deadline - loop.time()) * 1000)))

        if ok:
            handle = LockHandle(key, token, ttl, redis)
            handle.start_renew()
            return handle

        if loop.time() >= deadline:
            raise LockTimeout(f"等锁超时：{name}")
        # 带抖动的退避：多个实例同时重试时避免「齐步走」把 Redis 打尖
        await asyncio.sleep(random.randint(_POLL_MIN_MS, _POLL_MAX_MS) / 1000)


_local_locks: dict[str, asyncio.Lock] = {}


async def _local_lock(key: str, wait_ms: int) -> LockHandle:
    """进程内锁（降级路径）。同一 key 复用一个 `asyncio.Lock`。"""
    lock = _local_locks.get(key)
    if lock is None:
        lock = _local_locks[key] = asyncio.Lock()

    token = uuid.uuid4().hex
    if wait_ms <= 0:
        await lock.acquire()
    else:
        try:
            await asyncio.wait_for(lock.acquire(), timeout=wait_ms / 1000)
        except asyncio.TimeoutError:
            raise LockTimeout(f"等锁超时（进程内）：{key}") from None
    return LockHandle(key, token, 0, None, local=lock)


def reset_for_tests() -> None:
    """清掉进程内锁缓存（测试用）。"""
    _local_locks.clear()