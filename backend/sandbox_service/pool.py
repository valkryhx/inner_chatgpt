"""槽池：管理「每用户一个预热执行环境」。

## 为什么是「一用户一槽」而不是「一个槽服务多人」

方案 §4.7.1 实测：`bwrap --unshare-all` 用掉那份 `CAP_SYS_ADMIN` 后，
子进程无法再 `unshare(CLONE_NEWNS)` 重挂 workspace。所以：

```
槽 ──bwrap 启动时绑定 /ws──► 某个用户的 workspace（生命周期内不变）
```

**一个槽换不了用户。** 这也是为什么池的上限 = 同时能服务的人数。

## 三种 overflow 策略（方案 §4.7.3）

| 取值 | 槽满时 | 代价 |
| --- | --- | --- |
| `queue` | 等一个槽空出来（带超时） | 第 N+1 个用户变慢 |
| `evict` | 淘汰最久未用的**空闲**槽 | 被淘汰者下次要重新预热 |
| `reject` | 立刻报 SlotBusy | 用户直接看到失败 |

**三条硬规则（与 overflow 取值无关）**：

1. **忙碌槽永不淘汰** —— 否则掐断正在跑的代码
2. **淘汰前先归档** —— 否则丢用户文件（归档在调用方，这里只给回调）
3. **空闲 TTL 到期一律销毁** —— 不受 overflow 影响

三条销毁路径（`destroy` / `_evict` / `reap_idle`）走**同一个** `_drop_slot`，
保证「停 daemon」一条都不漏。漏掉的后果是**孤儿常驻解释器**：
单槽边际 ~53MB（预导入 pandas+matplotlib），空闲回收本该是主要出口，
漏在那里几天就能把容器撑到 OOM。

归档语义保持原样（只有 `_evict` 归档，见 `on_evict`）：
workspace 是卷上的真实目录，回收槽不会删它，所以 `reap_idle` 不需要额外打包。

## 纯净 fork（方案 §4.2 D）

槽里跑一个**只预热、永不执行用户代码**的父解释器；每次 exec 从它 `fork()`。
这样既能拿到「模块已在内存」的 8.8ms 好处，又不会跨用户泄漏
（父进程内存里不含任何用户数据）。

三条铁律写在本文件对应位置，改代码前先读。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

VALID_OVERFLOW = ("queue", "evict", "reject")
_DEFAULT_OVERFLOW = "queue"
_DEFAULT_QUEUE_TIMEOUT_S = 60.0
_DEFAULT_IDLE_TTL_S = 300.0


class SlotBusy(Exception):
    """没有可用槽（队列超时 / reject）。"""

    def __init__(self, reason: str, queue_wait_ms: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        # 方案 §4.7.4 第 4 条：排队时长要可观测，供调 MAX_SLOTS 用
        self.queue_wait_ms = queue_wait_ms


@dataclass
class Slot:
    """一个用户的执行环境。

    `parent_proc` 是**只预热、不执行用户代码**的父解释器（方案 §4.2 D）。
    `has_user_code_loaded` 恒为 False —— 它是给测试用的断言点：
    如果哪天有人图省事让父进程执行用户代码，这个标志会暴露它。
    """

    user_id: str
    workspace: Path
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    busy: bool = False
    parent_proc: object | None = None
    has_user_code_loaded: bool = False
    # 归档是否已在本槽上恢复过。**必须挂槽上，不能挂用户上**：
    # 槽被回收后再次 acquire 是新对象，那时应该恢复一次；
    # 挂在用户上会让新槽永远不恢复（同样是数据问题，方向相反）。
    archived_restored: bool = False

    def touch(self) -> None:
        self.last_used_at = time.monotonic()


class SlotPool:
    """按需扩缩容的槽池。

    并发模型：全部操作在同一事件循环里，用一把 `asyncio.Lock` 保护 `_slots`。
    不需要线程锁——池只在 async 上下文里被调用。
    """

    def __init__(
        self,
        *,
        max_slots: int,
        workspace_resolver: Callable[[str], Optional[Path]],
        overflow: str = _DEFAULT_OVERFLOW,
        queue_timeout_s: float = _DEFAULT_QUEUE_TIMEOUT_S,
        idle_ttl_s: float = _DEFAULT_IDLE_TTL_S,
        preload: str = "numpy,pandas,matplotlib",
        on_evict: Callable[[Slot], None] | None = None,
        prewarm: Callable[[str], Awaitable[object]] | None = None,
        on_destroy: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.max_slots = max(1, int(max_slots))
        self.overflow = self._normalize_overflow(overflow)
        self.queue_timeout_s = float(queue_timeout_s)
        self.idle_ttl_s = float(idle_ttl_s)
        self._resolve = workspace_resolver
        self._preload = preload
        self._on_evict = on_evict
        # 预热回调：建槽时调它起常驻解释器（None = 不预热，只记账）
        #
        # 返回值是 object 而不是 bool：`InprocExecutor.start()` 返回 None（它没有
        # daemon 可起）。用 bool 注解会逼调用方把 None 当失败，于是每次建槽都打
        # 一条「预热失败，退化到一次性模式」的假警告。判成功的口径统一在池里，
        # 见 `_prewarm_ok()`。
        self._prewarm = prewarm
        # 销毁回调：槽被销毁/淘汰/空闲回收时停掉它的常驻解释器。
        # 漏掉这一环 = 孤儿进程泄漏（每条路径都要走到，见 `_drop_slot`）。
        self._on_destroy = on_destroy

        self._slots: dict[str, Slot] = {}
        self._lock = asyncio.Lock()
        # 有槽空出来时通知等待者
        self._slot_freed = asyncio.Event()

    # ------------------------------------------------------------------ 配置

    @staticmethod
    def _normalize_overflow(raw: str) -> str:
        """非法取值退回 `queue`。

        为什么退回 queue 而不是 reject：退回 queue 保证请求**最终能成功**，
        退回 reject 会直接失败。配置写错不该让用户跑不了代码。
        （与 `HLZ_SANDBOX_MODE` 非法值退回 `off` 同类思路，但方向更保守。）
        """
        v = (raw or "").strip().lower()
        if v in VALID_OVERFLOW:
            return v
        logger.warning("非法 HLZ_SANDBOX_OVERFLOW=%r，退回 queue", raw)
        return _DEFAULT_OVERFLOW

    # ------------------------------------------------------------------ 查询

    def size(self) -> int:
        return len(self._slots)

    def users(self) -> set[str]:
        return set(self._slots)

    def snapshot(self) -> list[dict]:
        return [
            {
                "user_id": s.user_id,
                "workspace": str(s.workspace),
                "busy": s.busy,
                "idle_s": round(time.monotonic() - s.last_used_at, 1),
            }
            for s in self._slots.values()
        ]

    # ------------------------------------------------------------------ 获取

    async def acquire(self, user_id: str) -> Slot:
        """拿到该用户的槽；没有就按策略处理。

        Raises:
            SlotBusy: 槽满且策略不允许继续（queue 超时 / reject）。
        """
        started = time.monotonic()

        slot = self._slots.get(user_id)
        if slot is not None:
            slot.busy = True
            slot.touch()
            return slot

        # 有空间 → 直接建
        if len(self._slots) < self.max_slots:
            return await self._create(user_id)

        # 满了，按策略
        if self.overflow == "reject":
            raise SlotBusy(
                f"沙箱繁忙：已达上限 {self.max_slots}（overflow=reject）",
                queue_wait_ms=int((time.monotonic() - started) * 1000),
            )

        if self.overflow == "evict":
            victim = self._pick_lru_idle()
            if victim is not None:
                await self._evict(victim)
                return await self._create(user_id)
            # 全忙 → **退化成 queue**（忙碌槽永不淘汰，硬规则）
            logger.info("overflow=evict 但无空闲槽，退化成 queue（忙碌槽不许淘汰）")

        return await self._wait_then_create(user_id, started)

    async def _wait_then_create(self, user_id: str, started: float) -> Slot:
        """等一个槽空出来再建（queue 分支 + evict 的兜底）。

        ## 关键语义（这里绕了一个弯，写清楚）

        槽是**专属**的（§4.7.1），所以「某个槽 release 了」**不等于**「名额空出来了」——
        u1 的槽 release 之后仍然是 u1 的，仍然占着 `max_slots` 的名额。
        如果只等「release 事件」，u2 会一直等到超时（本实现第一版就是这个 bug，
        测试 `test_queue_waits_when_full` 抓出来了）。

        所以 queue 的正确语义是：
        **等到「有机会腾出名额」** —— 两种情况：
          a) 某个槽被 TTL 回收 / 显式销毁 → 名额直接空出
          b) 某个槽变成空闲 → **淘汰它**，名额腾出

        b) 让 queue 和 evict 在「全忙」时行为一致（都等），
        区别只在「当前就有空闲槽」时：evict 立刻淘汰，queue 也淘汰
        （因为不淘汰就永远等不到）。**两者对用户的实际差别是：
        evict 在看到空闲槽时立刻动手，queue 会先把机会留给「槽被销毁」。**
        单机槽少时两者接近，槽多时 evict 更有用（方案 §4.7.3）。
        """
        deadline = time.monotonic() + self.queue_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SlotBusy(
                    f"沙箱繁忙：等槽超时（{self.queue_timeout_s}s，"
                    f"上限 {self.max_slots}）",
                    queue_wait_ms=int((time.monotonic() - started) * 1000),
                )

            # 先把「此刻就能满足」的情况处理掉，避免无谓等待
            slot = self._slots.get(user_id)
            if slot is not None:
                slot.busy = True
                slot.touch()
                return slot
            if len(self._slots) < self.max_slots:
                return await self._create(user_id)

            # 满了：如果有空闲槽，淘汰它腾名额（queue 也不能干等）
            victim = self._pick_lru_idle()
            if victim is not None:
                await self._evict(victim)
                return await self._create(user_id)

            # 全忙 → 真的等，等到有人 release/销毁/TTL 回收
            self._slot_freed.clear()
            try:
                await asyncio.wait_for(self._slot_freed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                continue

    async def _create(self, user_id: str) -> Slot:
        ws = self._resolve(user_id)
        if ws is None:
            raise ValueError(f"无法解析用户 {user_id!r} 的 workspace")

        slot = Slot(user_id=user_id, workspace=Path(ws), busy=True)
        self._slots[user_id] = slot
        logger.info("建槽 user=%s workspace=%s（现有 %d/%d）",
                    user_id, slot.workspace, len(self._slots), self.max_slots)

        # ★ 真的预热：调 executor.start() 起常驻解释器。
        #
        #   这一步是「池」和「记账」的分界线。上一轮只往 slot.parent_proc
        #   塞了个 {"preload": ...} 字典——池看起来在工作（日志有建槽），
        #   但每次 exec 仍然是全新进程，性能收益为零（实测第二次 567ms）。
        #
        #   预热失败**不算建槽失败**：退化到一次性模式，慢一点但能用。
        #   把预热失败做成硬错误会让「容器里没装 bwrap」直接导致服务不可用。
        #
        # ★ 铁律 3：预导入清单固定，不按用户请求动态 import。
        #   按请求 import 会把用户数据带进父进程内存，而 fork 是写时复制，
        #   子进程能读到父进程的全部内存 → 跨用户泄漏。
        if self._prewarm is not None:
            try:
                started = time.monotonic()
                ok = self._prewarm_ok(await self._prewarm(user_id))
                slot.parent_proc = {"prewarm_ok": ok}
                slot.created_at = time.monotonic()
                if ok:
                    logger.info("槽预热完成 user=%s（%.0fms）",
                                user_id, (time.monotonic() - started) * 1000)
                else:
                    logger.warning("槽预热失败 user=%s，退化到一次性模式", user_id)
            except Exception:  # noqa: BLE001
                logger.warning("槽预热异常 user=%s，退化到一次性模式",
                               user_id, exc_info=True)
                slot.parent_proc = {"prewarm_ok": False}

        return slot

    @staticmethod
    def _prewarm_ok(result: object) -> bool:
        """把预热回调的返回值归一成「成功 / 失败」。

        ## 为什么不能直接 `if ok:`

        `InprocExecutor.start()` 返回 **None**（它没有 daemon 可起），
        `None` 是 falsy。直接判定会让 off/inproc 模式**每次建槽都打一条**
        「预热失败，退化到一次性模式」——日志被噪音填满，
        真正的失败反而淹在里面。

        口径：**只有显式 False 算失败**。None 表示「这个执行器不需要预热」，
        与「预热失败」是两件事。
        """
        return result is not False

    # ------------------------------------------------------------------ 释放 / 回收

    async def release(self, user_id: str) -> None:
        slot = self._slots.get(user_id)
        if slot is None:
            return
        slot.busy = False
        slot.touch()
        self._slot_freed.set()

    async def destroy(self, user_id: str) -> None:
        """销毁某用户的槽（幂等）。不负责归档——那是调用方的事。"""
        slot = self._slots.get(user_id)
        if slot is not None:
            await self._drop_slot(slot, reason="显式销毁")

    async def reap_idle(self) -> int:
        """回收空闲超时的槽。返回回收数量。

        **忙碌槽不回收**——否则会掐断正在跑的代码。
        """
        now = time.monotonic()
        victims = [
            s for s in self._slots.values()
            if not s.busy and (now - s.last_used_at) >= self.idle_ttl_s
        ]
        for s in victims:
            idle_s = now - s.last_used_at
            await self._drop_slot(s, reason=f"空闲回收（{idle_s:.0f}s）")
        return len(victims)

    def _pick_lru_idle(self) -> Slot | None:
        """最久未用的**空闲**槽。忙碌的一律不考虑。"""
        idle = [s for s in self._slots.values() if not s.busy]
        if not idle:
            return None
        return min(idle, key=lambda s: s.last_used_at)

    async def _evict(self, victim: Slot) -> None:
        """淘汰一个空闲槽。

        ★ 铁律 2：淘汰前先归档（通过 `on_evict` 回调交给上层）。
        归档逻辑不在这里实现——归档后端是可切换的（卷 / S3），
        不该耦合进池（方案 §7.4）。
        """
        if victim.busy:
            # 理论上前面已经过滤了；这里是最后一道防线
            raise RuntimeError(f"拒绝淘汰忙碌槽 {victim.user_id}")
        if self._on_evict is not None:
            try:
                self._on_evict(victim)
            except Exception:  # noqa: BLE001
                logger.warning("归档回调失败（继续淘汰）user=%s", victim.user_id,
                               exc_info=True)
        await self._drop_slot(victim, reason="淘汰（LRU 空闲）")

    async def _drop_slot(self, slot: Slot, *, reason: str) -> None:
        """把槽从表里摘掉，并**停掉它的常驻解释器**。

        ## 为什么三条路径必须共用一个出口

        停 daemon 这一步极易漏：`destroy` 看起来该做、`_evict` 有归档回调
        掩护着容易忘、`reap_idle` 更是只像「清内存」。漏掉的后果是**孤儿进程**：
        daemon 是 bwrap 拉起的常驻 python，父进程不 kill 就一直活着，
        单槽边际 ~53MB。空闲回收恰恰是最频繁的出口，漏在那里几天就 OOM。

        ## 顺序：先摘表，再停进程

        先摘表让名额立刻可用（`_slot_freed.set()` 唤醒等待者），再去做
        「可能慢」的 kill。反过来的话，等槽的请求要多等一次 kill 的时间。
        """
        if self._slots.pop(slot.user_id, None) is None:
            return                     # 已被别处摘掉，幂等返回
        logger.info("销毁槽 user=%s（%s）", slot.user_id, reason)
        self._slot_freed.set()

        if self._on_destroy is not None:
            try:
                await self._on_destroy(slot.user_id)
            except Exception:  # noqa: BLE001
                # 停不掉 daemon 不该让销毁失败——槽已经从表里摘掉了，
                # 名额已释放。进程泄漏比「销毁报错导致状态不一致」轻。
                logger.warning("停 daemon 失败 user=%s", slot.user_id, exc_info=True)