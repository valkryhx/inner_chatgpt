"""跨实例插话队列（steer queue）的 Redis 总线。

## 问题

```
浏览器 -> LB -> 实例 B  POST /queue  (用户中途补充要求)
                  B: 写进 B 内存里的 SteerQueue
              生成跑在实例 A
                  A: drain 自己的内存队列 -> 空 -> 用户那条消息丢了
```

## 为什么不能让 drain 直接查 Redis

`drain` 在 `sse.py:124` 的 **同步注入回调** `_inject()` 里被调用
（`rebuild_message_injector` 的签名就是同步的），它拿不到 await。
改成 async 会牵动消息注入器整条链。

## 做法：与 stopsignal 同构的两级

1. **push 侧**：配了 Redis 时，`SteerQueue.push` 只往 Redis list 写
   （`RPUSH hlz:steer:<u>:<s>:<g>`），不写本地 —— 避免同一实例重复入队。
2. **拉取侧**：生成所在的实例起一个后台协程，定期把 Redis list 里的条目
   搬进本地 `SteerQueue`。`drain()` 一字未改，仍然读本地队列。

延迟 = 轮询间隔（默认 200ms）。对「用户中途补充一句」这个场景完全够用，
而且不影响首 token（插话本来就是在流式过程中注入）。

## 没有 Redis 时

`enabled()` 为 False，`push` 走原来的「直接写本地队列」路径，
行为与迁移前完全一致。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from .client import get_redis
from .config import cluster_enabled

logger = logging.getLogger(__name__)

# 队列存活时间：一次生成最长也就几十分钟，24h 足够且不会永久堆积
_LIST_TTL_MS = 24 * 3600 * 1000
# 拉取间隔。200ms 对插话场景足够快，又不至于把 Redis 打尖。
_POLL_S = 0.2
# 一次最多搬多少条（与 MAX_QUEUE_DEPTH 对齐，多搬也没意义）
_MAX_BATCH = 50


def enabled() -> bool:
    return cluster_enabled()


def _key(user_id: str, session_id: str, generation_id: str) -> str:
    prefix = os.environ.get("HLZ_REDIS_PREFIX", "hlz").strip() or "hlz"
    return f"{prefix}:steer:{user_id}:{session_id}:{generation_id}"


async def push(user_id: str, session_id: str, generation_id: str,
               item: dict[str, Any]) -> bool:
    """把一条插话写进 Redis list。返回是否成功。"""
    if not enabled():
        return False
    redis = await get_redis()
    if redis is None:
        return False
    import json

    key = _key(user_id, session_id, generation_id)
    try:
        await redis.rpush(key, json.dumps(item, ensure_ascii=False))
        await redis.pexpire(key, _LIST_TTL_MS)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("插话入 Redis 失败", exc_info=True)
        return False


async def pop_batch(user_id: str, session_id: str, generation_id: str,
                    limit: int = _MAX_BATCH) -> list[dict[str, Any]]:
    """原子取走若干条（LPOP count）。返回解析后的条目。"""
    if not enabled():
        return []
    redis = await get_redis()
    if redis is None:
        return []
    import json

    key = _key(user_id, session_id, generation_id)
    try:
        raw = await redis.lpop(key, max(1, min(int(limit), _MAX_BATCH)))
        if not raw:
            return []
        if isinstance(raw, str):
            raw = [raw]
        out: list[dict[str, Any]] = []
        for s in raw:
            try:
                out.append(json.loads(s))
            except Exception:  # noqa: BLE001 - 坏条目跳过，别卡住整队
                continue
        return out
    except Exception:  # noqa: BLE001
        logger.warning("插话出 Redis 失败", exc_info=True)
        return []


async def clear(user_id: str, session_id: str, generation_id: str) -> None:
    if not enabled():
        return
    redis = await get_redis()
    if redis is None:
        return
    try:
        await redis.delete(_key(user_id, session_id, generation_id))
    except Exception:  # noqa: BLE001
        pass


async def depth(user_id: str, session_id: str, generation_id: str) -> int:
    if not enabled():
        return 0
    redis = await get_redis()
    if redis is None:
        return 0
    try:
        return int(await redis.llen(_key(user_id, session_id, generation_id)) or 0)
    except Exception:  # noqa: BLE001
        return 0


class SteerPump:
    """把 Redis 里的插话搬进本地 SteerQueue 的后台协程。"""

    __slots__ = ("_queue", "_scope", "_task", "_stopping")

    def __init__(self, queue, user_id: str, session_id: str,
                 generation_id: str) -> None:
        self._queue = queue
        self._scope = (user_id, session_id, generation_id)
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while not self._stopping:
                items = await pop_batch(*self._scope)
                for item in items:
                    # 直接塞进本地队列的底层列表：`push` 在 Redis 模式下
                    # 会再往 Redis 写一次，那就绕成环了。
                    self._queue._adopt(item)
                await asyncio.sleep(_POLL_S)
        except asyncio.CancelledError:
            pass

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