"""每用户容器注册表 + 懒创建 + TTL 回收 + 全局上限 LRU。

设计文档 §6.2（生命周期）、§9.1-R3（同用户多会话共享容器）。

**只管「有几个容器、谁在用」，不管「怎么执行」** —— 后者是 ``runtime``。
这个划分让改 TTL 逻辑不必碰 exec 代码（§6.1 已论证）。

关键不变量：
1. 同一 ``user_id`` 在注册表里最多一条记录，多个并发 ``acquire`` 复用同一个容器；
2. 创建失败不留下半成品记录（避免"注册表说有、实际没有"）；
3. 全局数量超过 ``max_containers`` 时按 LRU 淘汰，**但从不淘汰正在被使用的**。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .config import SandboxConfig, get_config
from .errors import SandboxExecError, SandboxUnavailable
from .runtime import ContainerRuntime

logger = logging.getLogger(__name__)


@dataclass
class ContainerEntry:
    """一个用户的容器记录。"""

    user_id: str
    container_id: str
    created_at: float
    last_used: float
    # 正在执行的并发数；>0 时不允许被 LRU 淘汰（否则会杀掉正在跑的任务）
    in_flight: int = 0
    host_workspace: str = ""


class ContainerPool:
    """每用户容器池。线程/协程安全（用 ``asyncio.Lock`` 保护注册表）。"""

    def __init__(self, cfg: SandboxConfig | None = None,
                 runtime: ContainerRuntime | None = None) -> None:
        self.cfg = cfg or get_config()
        self.runtime = runtime or ContainerRuntime(self.cfg)
        self._entries: dict[str, ContainerEntry] = {}
        self._lock = asyncio.Lock()

    # ---------------- 查询 ----------------

    def size(self) -> int:
        return len(self._entries)

    def get_entry(self, user_id: str) -> ContainerEntry | None:
        return self._entries.get(user_id)

    def snapshot(self) -> list[ContainerEntry]:
        """供诊断/日志用（不持有锁，只读快照）。"""
        return list(self._entries.values())

    # ---------------- 获取 ----------------

    async def acquire(self, user_id: str, host_workspace: str) -> ContainerEntry:
        """拿到该用户的可用容器；不存在或已死则创建。

        并发安全：两个协程同时为同一用户 acquire，只会创建一个容器
        （锁内完成"检查 → 创建 → 登记"）。
        """
        async with self._lock:
            entry = self._entries.get(user_id)
            if entry is not None:
                okay = await asyncio.to_thread(self.runtime.is_alive, entry.container_id)
                if okay:
                    entry.last_used = time.monotonic()
                    entry.in_flight += 1
                    return entry
                # 容器死了：清掉记录，走重建
                logger.warning("沙箱容器已失效，重建 user=%s cid=%s",
                               user_id, entry.container_id[:12])
                self._entries.pop(user_id, None)

            # 全局上限：先腾位置（只淘汰 in_flight == 0 的）
            await self._evict_if_needed_locked()

            cid = await asyncio.to_thread(self.runtime.create, user_id, host_workspace)
            now = time.monotonic()
            entry = ContainerEntry(
                user_id=user_id, container_id=cid, created_at=now, last_used=now,
                in_flight=1, host_workspace=host_workspace,
            )
            self._entries[user_id] = entry
            logger.info("沙箱容器就绪 user=%s cid=%s（池内 %d 个）",
                        user_id, cid[:12], len(self._entries))
            return entry

    async def release(self, entry: ContainerEntry) -> None:
        """执行结束，归还。（不销毁容器，只是减并发计数）"""
        async with self._lock:
            entry.in_flight = max(0, entry.in_flight - 1)
            entry.last_used = time.monotonic()

    # ---------------- 淘汰 / 回收 ----------------

    async def _evict_if_needed_locked(self) -> None:
        """调用方必须已持有 ``self._lock``。"""
        while len(self._entries) >= self.cfg.max_containers:
            idle = [e for e in self._entries.values() if e.in_flight == 0]
            if not idle:
                # 全都在跑：不能淘汰，只能让本次创建排队失败
                raise SandboxExecError(
                    f"沙箱容器已达上限 {self.cfg.max_containers}，且全部正在使用；"
                    "请稍后重试或提高 HLZ_SANDBOX_MAX_CONTAINERS"
                )
            victim = min(idle, key=lambda e: e.last_used)
            logger.info("LRU 淘汰沙箱容器 user=%s cid=%s（空闲 %.0fs）",
                        victim.user_id, victim.container_id[:12],
                        time.monotonic() - victim.last_used)
            self._entries.pop(victim.user_id, None)
            try:
                await asyncio.to_thread(self.runtime.destroy, victim.container_id)
            except Exception:  # noqa: BLE001
                logger.warning("淘汰时销毁容器失败", exc_info=True)

    async def reap_idle(self) -> int:
        """销毁空闲超过 TTL 的容器。返回销毁数量。后台协程周期性调用。"""
        now = time.monotonic()
        async with self._lock:
            expired = [
                e for e in self._entries.values()
                if e.in_flight == 0 and (now - e.last_used) > self.cfg.idle_ttl_s
            ]
            for e in expired:
                self._entries.pop(e.user_id, None)
        # 销毁放在锁外，避免长时间持锁
        for e in expired:
            try:
                await asyncio.to_thread(self.runtime.destroy, e.container_id)
                logger.info("TTL 回收沙箱容器 user=%s cid=%s", e.user_id, e.container_id[:12])
            except Exception:  # noqa: BLE001
                logger.warning("TTL 回收销毁失败", exc_info=True)
        return len(expired)

    async def destroy_user(self, user_id: str) -> bool:
        """主动销毁某用户的容器（供 /stop 或诊断用）。"""
        async with self._lock:
            entry = self._entries.pop(user_id, None)
        if entry is None:
            return False
        try:
            await asyncio.to_thread(self.runtime.destroy, entry.container_id)
            return True
        except Exception:  # noqa: BLE001
            logger.warning("销毁用户容器失败", exc_info=True)
            return False

    async def shutdown(self) -> int:
        """应用关闭：销毁所有容器。返回销毁数量。"""
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for e in entries:
            try:
                await asyncio.to_thread(self.runtime.destroy, e.container_id)
            except Exception:  # noqa: BLE001
                logger.warning("shutdown 销毁容器失败", exc_info=True)
        if entries:
            logger.info("shutdown：已销毁 %d 个沙箱容器", len(entries))
        return len(entries)


# 进程内单例（与 manager 共享同一个池）
_pool: ContainerPool | None = None


def get_pool(*, reload: bool = False) -> ContainerPool:
    global _pool
    if _pool is None or reload:
        _pool = ContainerPool()
    return _pool


async def reap_loop(interval_s: float = 60.0) -> None:
    """后台回收协程。由 manager.start() 起为 task，shutdown() 取消。"""
    pool = get_pool()
    while True:
        try:
            await asyncio.sleep(interval_s)
            n = await pool.reap_idle()
            if n:
                logger.info("TTL 回收 %d 个容器", n)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("TTL 回收循环出错（继续）", exc_info=True)