"""跨实例的停止信号：用 Redis set + 本地订阅协程。

## 问题

用户在实例 B 点「停止」，但生成跑在实例 A 上：

```
浏览器 -> LB -> 实例 B  POST /stop
                 B: runtime._stop_events 里没有这个 key（A 的内存里）
                 -> 返回 ok，但什么都没停
```

## 为什么不能直接把 `stopped()` 改成查 Redis

`stopped()` 在流式回调里**每收到一个 token 就调一次**
（`agent_loop.py:455` -> `llm_client` 的 `stop_check`）。那里必须是
同步且零开销的函数，不能 await、更不能每次都发一次 Redis 往返。

## 做法：两级

1. **发布侧**（`request_stop`）：`SET hlz:stop:<user>:<sess>:<gen> 1 PX ttl`
2. **订阅侧**（生成开始时起一个协程）：轮询那个 key（默认 1s），
   一旦发现就 `local_event.set()` —— 之后 `stopped()` 照旧读本地 Event，
   零改动。

轮询而不是 pubsub：pubsub 在重连时会丢消息，而「停止」丢了就是用户
点了没反应。1s 的轮询延迟可以接受（用户感知的是「很快就停了」），
而正确性有保证。

## 没有 Redis 时

`publish()` 是空操作，`watch()` 返回 None，调用方继续用进程内 `_stop_events`。
单实例行为完全不变。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from .client import get_redis
from .config import cluster_enabled

logger = logging.getLogger(__name__)

# 停止标记的存活时间：足够覆盖一次生成，又不会永久占 key
_STOP_TTL_MS = 300_000
# 订阅轮询间隔
_POLL_S = 1.0


def _key(user_id: str, session_id: str, generation_id: str) -> str:
    prefix = os.environ.get("HLZ_REDIS_PREFIX", "hlz").strip() or "hlz"
    return f"{prefix}:stop:{user_id}:{session_id}:{generation_id}"


async def publish(user_id: str, session_id: str, generation_id: str) -> bool:
    """在 Redis 里打一个停止标记。返回是否成功（没配 Redis = False）。"""
    if not cluster_enabled():
        return False
    redis = await get_redis()
    if redis is None:
        return False
    try:
        await redis.set(_key(user_id, session_id, generation_id), "1", px=_STOP_TTL_MS)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("发布停止标记失败", exc_info=True)
        return False


async def clear(user_id: str, session_id: str, generation_id: str) -> None:
    """清掉标记（生成正常结束后调用，避免下一次同 id 生成被误停）。"""
    if not cluster_enabled():
        return
    redis = await get_redis()
    if redis is None:
        return
    try:
        await redis.delete(_key(user_id, session_id, generation_id))
    except Exception:  # noqa: BLE001
        pass


class StopWatcher:
    """后台轮询 Redis，发现停止标记就 set 本地 Event。"""

    __slots__ = ("_event", "_task", "_key", "_stopping")

    def __init__(self, event: asyncio.Event, key: str) -> None:
        self._event = event
        self._key = key
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        redis = await get_redis()
        if redis is None:
            return
        try:
            while not self._stopping and not self._event.is_set():
                try:
                    if await redis.exists(self._key):
                        self._event.set()
                        return
                except Exception:  # noqa: BLE001 - 抖动不该让停止失效
                    logger.debug("停止标记轮询失败", exc_info=True)
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


async def watch(user_id: str, session_id: str, generation_id: str,
                event: asyncio.Event) -> Optional[StopWatcher]:
    """给本地 Event 挂一个跨实例监听。没配 Redis 返回 None。"""
    if not cluster_enabled():
        return None
    redis = await get_redis()
    if redis is None:
        return None
    w = StopWatcher(event, _key(user_id, session_id, generation_id))
    w.start()
    return w