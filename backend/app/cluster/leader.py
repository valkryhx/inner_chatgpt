"""Leader election：带租约 + 防惊群。

## 用它干什么

启动期的一次性动作**只能有一个实例执行**：

- 清扫本机残留的沙箱容器（`probe.cleanup_orphans(same_machine=True)`）——
  多实例时会把**另一个实例正在用**的容器删掉（`manager.py` 里已经写了这个警告）；
- `recover_stale_runtime()` —— 会把另一个实例正在跑的 generation 标成 stale；
- 未来的迁移/建表。

## 三个设计要点

### 1. 租约（TTL）而不是心跳计数器
持有者崩溃后锁必须自动失效，否则整个集群永远没人做启动动作。
用 `SET NX PX ttl` + 定期 `PEXPIRE`，进程死了 ttl 到了别人自然接手。

### 2. 防惊群（thundering herd / anti-herd）
租期到期的**那一瞬间**，所有 follower 会同时来抢。加一个随机化的
`refresh` 抖动（±2s）让它们错开。LibreChat 的 `LeaderElection` 同样这么做。

### 3. 只在「成为 leader 的那一刻」执行启动动作
不要每轮循环都执行。所以这里用回调（`on_become_leader`）而不是「查询式
`is_leader`」——查询式会让调用方自己决定什么时候跑，容易写成每 tick 都跑。

## 没有 Redis 时

单实例，`is_leader` 恒为 `True`，回调在 `start()` 时立即执行一次。
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
from typing import Awaitable, Callable, Optional

from .client import get_redis
from .config import leader_refresh_ms, leader_ttl_ms

logger = logging.getLogger(__name__)

# 续约：仍要校验 token，避免把别人的租约续上
_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# 防惊群：续约点抖动 ±2s（LibreChat 的做法）
_HERD_JITTER_S = 2.0


class LeaderElector:
    """带租约的 leader election。

    生命周期：
        elector = get_elector()
        elector.on_become_leader("sandbox-cleanup", my_async_fn)
        await elector.start()      # 启动后台循环（幂等）
        ...
        await elector.stop()       # 应用 shutdown
    """

    def __init__(self, name: str = "hlaudz") -> None:
        self.name = name
        self._token = uuid.uuid4().hex
        self._is_leader = False
        self._task: Optional[asyncio.Task] = None
        self._callbacks: dict[str, Callable[[], Awaitable[None]]] = {}
        self._fired: set[str] = set()
        self._stopping = False
        self._redis = None

    # ---------------- 回调 ----------------

    def on_become_leader(self, key: str, fn: Callable[[], Awaitable[None]]) -> None:
        """注册「成为 leader 时执行一次」的回调。同 key 只执行一次。"""
        self._callbacks[key] = fn

    async def _fire(self) -> None:
        for key, fn in list(self._callbacks.items()):
            if key in self._fired:
                continue
            self._fired.add(key)
            try:
                await fn()
            except Exception:  # noqa: BLE001 - 单个回调失败不该影响其他
                logger.warning("leader 回调失败：%s", key, exc_info=True)

    # ---------------- 状态 ----------------

    @property
    def is_leader(self) -> bool:
        # 没配 Redis = 单实例 = 永远是 leader
        if self._redis is None:
            return True
        return self._is_leader

    # ---------------- 生命周期 ----------------

    def _key(self) -> str:
        prefix = os.environ.get("HLZ_REDIS_PREFIX", "hlz").strip() or "hlz"
        return f"{prefix}:leader:{self.name}"

    async def start(self) -> None:
        """启动选举循环（幂等）。没配 Redis 时立即执行一次回调。"""
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._redis = await get_redis()

        if self._redis is None:
            # 单实例：直接当 leader，回调跑一次
            self._is_leader = True
            await self._fire()
            logger.info("未配置 Redis：单实例模式，跳过 leader 选举")
            return

        self._task = asyncio.create_task(self._loop())
        logger.info("leader 选举循环已启动（ttl=%dms refresh=%dms）",
                    leader_ttl_ms(), leader_refresh_ms())

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._redis is not None and self._is_leader:
            try:
                await self._redis.eval(_RELEASE_LUA, 1, self._key(), self._token)
            except Exception:  # noqa: BLE001
                pass
        self._is_leader = False

    async def _loop(self) -> None:
        ttl = leader_ttl_ms()
        base = leader_refresh_ms()
        key = self._key()

        while not self._stopping:
            try:
                if not self._is_leader:
                    ok = await self._redis.set(key, self._token, nx=True, px=ttl)
                    if ok:
                        self._is_leader = True
                        logger.info("成为 leader：%s", self.name)
                        await self._fire()
                else:
                    ok = await self._redis.eval(_RENEW_LUA, 1, key, self._token, ttl)
                    if not ok:
                        self._is_leader = False
                        # 重新成为 leader 时回调要不要再跑一次？
                        # **不跑** —— 一次性动作（清扫残留）重复执行有副作用。
                        # 需要「每次上任都跑」的动作应该自己判断幂等。
                        logger.warning("leader 租约丢失：%s", self.name)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - Redis 抖动不该让循环退出
                self._is_leader = False
                logger.warning("leader 选举异常，转 follower", exc_info=True)

            # 防惊群：refresh 加 ±2s 抖动，避免所有实例同一刻来抢
            sleep_s = max(base / 1000, 0.5) + random.uniform(-_HERD_JITTER_S, _HERD_JITTER_S)
            await asyncio.sleep(max(sleep_s, 0.2))


_elector: Optional[LeaderElector] = None


def get_elector() -> LeaderElector:
    global _elector
    if _elector is None:
        _elector = LeaderElector()
    return _elector


def reset_for_tests() -> None:
    global _elector
    _elector = None