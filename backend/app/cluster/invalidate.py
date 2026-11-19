"""跨实例的 runtime 缓存失效广播。

## 问题（这是 MCP 在多实例下真正的痛点）

`RuntimeHub._runtimes` 缓存着每个用户的 `UserRuntime`，里面装着：
- **活跃的 MCP 连接**（`MCPToolset._client` 是进程内的 fastmcp Client）
- 系统提示词（含 skills 的 L1 索引，构建时烘焙进去的）
- toolset / client 等重对象

用户在实例 B 改了 MCP 配置 / LLM 配置 / 装了新 skill 时，`me.py` 和
`skills.py` 会调 `hub.invalidate(user_id)` —— **但那只清 B 自己的缓存**。

实例 A 手里的旧连接会一直用下去，直到：
- `MCP_STATUS_TTL_S`（默认 60s）到期后某次 `refresh_mcp_status` 发现状态变了
- 或者连接自然断开触发 `reconnect`

也就是说：**改了配置最多 60 秒才生效，而且只是"可能"生效**（取决于那个用户
在 A 上有没有请求触发了状态刷新）。

## 为什么不能直接共享 MCP 连接

MCP 的 `Client` 是进程内对象（持 socket / 子进程 stdio），**跨进程无法共享**。
LibreChat 走得更远：它把 app-shared MCP 会话做成需要 `acquireLease`
保护的跨副本资源（`flow/manager.ts` 的 Redis Lua CAS），因为 OAuth refresh
token 会被并发重放导致整个授权族被吊销（RFC 9700 §4.13.2）。

**Hlaudz 的情况不同**：MCP headers 是静态配置（`mcp_config.py`），没有
OAuth refresh flow，所以不存在"重放吊销"这类风险。真正的代价只是
**缓存不同步**。

## 做法：失效广播

B 改配置 -> 写 `hlz:invalidate:<user_id>` 一个递增版本号
A 有个后台协程轮询自己的"已知版本" -> 发现变了就 `invalidate(user_id)`

轮询而不是 pubsub：pubsub 重连丢消息，而"配置改了没生效"用户会以为 bug。
2s 的延迟完全可以接受（配置变更本来就不是实时需求）。

## 粒度：按 user

一个用户改配置只影响他自己。不需要 invalidate_all 的跨实例传播 ——
那个是"共享 skill 导入"用的，影响所有用户，但那种操作频率极低，
可以让各实例在 TTL 后自然收敛（且它已经在代码里是显式运维动作）。

## 没有 Redis 时

`publish()` 空操作，`watcher` 不启动。单实例行为完全不变。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from .client import get_redis
from .config import cluster_enabled

logger = logging.getLogger(__name__)

# 版本号 key 的存活时间：一天足够（用户改配置不会一天之后才去看效果）
_KEY_TTL_MS = 24 * 3600 * 1000
# 轮询间隔：配置变更不是实时需求，2s 让 Redis 压力可以忽略
_POLL_S = 2.0


def _key(user_id: str) -> str:
    prefix = os.environ.get("HLZ_REDIS_PREFIX", "hlz").strip() or "hlz"
    return f"{prefix}:invalidate:{user_id}"


async def publish(user_id: str) -> bool:
    """把某个用户的缓存标记为「需要失效」。返回是否成功。

    用 `INCR` 而不是 `SET 1`：这样每次变更都产生一个新值，
    订阅方比对「我见过的最后一个值」就能发现**任何一次**变更
    （即使两次变更间隔很短）。
    """
    if not cluster_enabled():
        return False
    redis = await get_redis()
    if redis is None:
        return False
    key = _key(user_id)
    try:
        await redis.incr(key)
        await redis.pexpire(key, _KEY_TTL_MS)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("失效广播失败", exc_info=True)
        return False


async def current_version(user_id: str) -> Optional[int]:
    """当前失效版本号。没配 Redis / 没这个 key 返回 None。"""
    if not cluster_enabled():
        return None
    redis = await get_redis()
    if redis is None:
        return None
    try:
        raw = await redis.get(_key(user_id))
        return int(raw) if raw is not None else None
    except Exception:  # noqa: BLE001
        return None


class InvalidateWatcher:
    """轮询一个用户的失效版本号，变了就回调。"""

    __slots__ = ("_user_id", "_on_change", "_task", "_stopping", "_seen",
                 "_baseline_ready")

    def __init__(self, user_id: str, on_change) -> None:
        self._user_id = user_id
        self._on_change = on_change
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._seen: Optional[int] = None
        self._baseline_ready = False

    def start(self, baseline: Optional[int] = None) -> None:
        if self._task is None or self._task.done():
            self._seen = baseline
            # ⚠️ 必须用**独立标志**区分「基线还没建立」和「基线是 None」。
            #
            # baseline=None 的含义是「启动时 Redis 里没有这个 key」，这是
            # **有效基线**：之后只要读到任何值，就说明发生过一次 publish。
            #
            # 早期版本用 `if self._seen is None: 当基线`，导致
            # 「启动时无 key -> 别人 publish -> 读到 1」被当成基线吞掉，
            # 那次变更永久丢失（测试里 100% 复现）。
            self._baseline_ready = True
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while not self._stopping:
                await asyncio.sleep(_POLL_S)
                if self._stopping:
                    return
                v = await current_version(self._user_id)
                # key 不存在（v 为 None）本身不是变更信号：
                # 可能是被别人 TTL 清掉，也可能一直就没变更过。
                if v is None:
                    continue
                if not self._baseline_ready:
                    # 只有在没有显式给 baseline 的情况下才会走到这里
                    self._seen = v
                    self._baseline_ready = True
                    continue
                if v != self._seen:
                    self._seen = v
                    try:
                        await self._on_change(self._user_id)
                    except Exception:  # noqa: BLE001
                        logger.warning("跨实例失效回调失败 user=%s",
                                       self._user_id, exc_info=True)
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