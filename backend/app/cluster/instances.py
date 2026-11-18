"""instance_registry：登记「本机有哪些活着的实例」。

## 解决的问题

启动清扫按机器名过滤，单实例时安全。但**同机跑两个后端进程**时：

```
t=0  实例 A 启动，建容器 hlz_sbx_u1_A     (owner = host:111)
t=5  实例 B 启动，清扫 same_machine
     -> 看到 hlz_sbx_u1_A 的 owner 是 host:111，以 host: 开头
     -> 判定为「本机残留」，删掉
     -> A 正在这个容器里跑用户代码，炸
```

「本机」这个粒度不够细，需要知道**哪些 owner 还活着**。

## 做法

每个实例启动时把 `owner_id()` 写进 Redis 的 `hlz:instances:<machine>` 有序集合
（score = 心跳时间戳 ms），定期刷新，退出时删除。

清扫时：本机上「owner 不在当前存活集合里」的容器才算残留。

## 没有 Redis 时

单实例，`live_owners()` 返回空集合，调用方退化成「按机器名前缀匹配」
（迁移前的行为），语义不变 —— 因为单实例下本机确实只有自己。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from .client import get_redis
from .config import cluster_enabled
from ..sandbox.identity import owner_id, owner_machine

logger = logging.getLogger(__name__)

# 心跳超过这个时长就算死了（实例被 kill -9 时不会走退出清理）
_STALE_MS = 90_000


def _key(machine: str) -> str:
    prefix = os.environ.get("HLZ_REDIS_PREFIX", "hlz").strip() or "hlz"
    return f"{prefix}:instances:{machine}"


async def register_self() -> bool:
    """登记本实例（幂等）。返回是否登记成功（没 Redis = False）。"""
    if not cluster_enabled():
        return False
    redis = await get_redis()
    if redis is None:
        return False
    key = _key(owner_machine())
    now = int(time.time() * 1000)
    try:
        await redis.zadd(key, {owner_id(): now})
        await redis.pexpire(key, _STALE_MS * 2)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("登记实例失败", exc_info=True)
        return False


async def heartbeat() -> None:
    """刷新本实例的心跳时间。由后台协程定期调用。"""
    if not cluster_enabled():
        return
    redis = await get_redis()
    if redis is None:
        return
    key = _key(owner_machine())
    now = int(time.time() * 1000)
    try:
        await redis.zadd(key, {owner_id(): now})
        # 顺手清掉死掉的实例，避免集合无限增长
        await redis.zremrangebyscore(key, 0, now - _STALE_MS)
        await redis.pexpire(key, _STALE_MS * 2)
    except Exception:  # noqa: BLE001
        logger.debug("实例心跳失败", exc_info=True)


async def unregister_self() -> None:
    """退出时把自己从登记表移除（正常 shutdown 走这条路）。"""
    if not cluster_enabled():
        return
    redis = await get_redis()
    if redis is None:
        return
    try:
        await redis.zrem(_key(owner_machine()), owner_id())
    except Exception:  # noqa: BLE001
        pass


async def live_owners(machine: Optional[str] = None) -> set[str]:
    """本机（或指定机器）当前**活着**的 owner 集合。

    没配 Redis 返回空集合 —— 调用方据此退化成「按机器名前缀」的宽松模式。
    这正是单实例的正确语义（本机只有自己，所有本机容器都算残留）。
    """
    if not cluster_enabled():
        return set()
    redis = await get_redis()
    if redis is None:
        return set()
    machine = machine or owner_machine()
    now = int(time.time() * 1000)
    try:
        rows = await redis.zrangebyscore(
            _key(machine), now - _STALE_MS, "+inf")
        return set(rows or [])
    except Exception:  # noqa: BLE001
        logger.warning("读取存活实例失败", exc_info=True)
        return set()