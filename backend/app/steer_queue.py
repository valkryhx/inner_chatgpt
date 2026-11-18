"""用户中途插话队列（steering）。

场景：母代理/子代理正在跑，用户在界面上补一句要求，希望它在**这一次生成**里
一并处理，而不是被 409 挡回去另起一轮。

设计要点（对应 docs/20261111-用户中途插话steering实施方案.md §3.2）：

1. **队列挂在 generation 注册表上，不挂在 TurnRuntime 上。**
   GA 的坑：最后一条消息可能晚于最后一个 fold 点到达，必须能活到下一回合；
   挂在回合对象上会随回合一起消失。
2. **drain 必须原子。** HTTP handler 与 SSE 生成器都在同一个事件循环里，
   用「整体替换列表」实现取走即清，避免读-清两步丢掉中间到达的消息。
3. **注入文本要带包装文案**，否则模型会把补充要求当成新问题重答一遍。
"""
from __future__ import annotations

import itertools
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# 单条插话上限。插话是补一句要求，不是二次投喂整篇文档。
MAX_STEER_CHARS = 4000
# 队列深度上限。防止客户端刷屏把下一轮上下文撑爆。
MAX_QUEUE_DEPTH = 20


def wrap_user_steer(text: str) -> str:
    """把用户插话包装成「执行过程中的补充要求」。

    措辞照 CC 的 queued_command 思路：先说明这是中途补充，再明确要求
    「不要当成新回合、不要重新回答最初的问题」——不写这句，模型很容易
    把整件事从头再做一遍。
    """
    return (
        "[用户在你执行过程中补充的消息]\n"
        f"{text}\n\n"
        "这是用户在上一轮执行过程中补充的要求，请在本回合内一并处理，"
        "不要当成一个新的对话回合，也不要重新回答最开始的问题。"
    )


@dataclass
class SteerQueue:
    """一个 generation 的插话队列。"""

    _items: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _seq: Any = field(default_factory=lambda: itertools.count(1))
    # (user_id, session_id, generation_id)。Redis 模式与后台泵都要用。
    # 用 field(default=None) 而不是构造参数，保持 SteerQueue() 无参可用
    # （测试里大量这么用）。
    _scope: Any = field(default=None, compare=False)

    def push(self, text: Any, *, source: str = "user", priority: str = "next") -> int:
        """入队，返回队列深度；被拒绝时返回 0。

        ## 多实例行为（2026-11-19）

        配了 Redis 时**只写 Redis**，不写本地 —— 生成可能跑在别的实例上，
        写本地等于丢消息。本实例（如果它正是生成方）由一个后台泵把条目
        搬回本地队列（见 `cluster/steerbus.py`）。

        没配 Redis 时走原来的「直接写本地」路径，行为与迁移前完全一致。
        """
        clean = " ".join(str(text or "").split()) if str(text or "").strip() else ""
        if not clean:
            return 0
        if len(clean) > MAX_STEER_CHARS:
            clean = clean[:MAX_STEER_CHARS]

        item = {
            "id": "steer_" + uuid.uuid4().hex[:12],
            "seq": 0,                 # Redis 模式下由泵在本地分配
            "text": clean,
            "source": source,
            "priority": priority,
            "created_at": time.time(),
        }

        try:
            from .cluster import steerbus
            if steerbus.enabled():
                # 深度要先看本地（泵可能已经搬了一些）+ 远端
                if len(self) >= MAX_QUEUE_DEPTH:
                    return 0
                _spawn_remote_push(self._scope, item)
                # 立即返回一个乐观深度：确切的远端深度要 await，
                # 而 push 是同步的。前端只用它判断「有没有排上」。
                return len(self) + 1
        except Exception:  # noqa: BLE001 - 总线不可用时退回本地
            pass

        return self._adopt(item)

    def _adopt(self, item: dict[str, Any]) -> int:
        """把一条（来自本地或 Redis 泵的）条目真正放进本地队列。

        返回入队后的深度；队列满时返回 0。
        """
        with self._lock:
            if len(self._items) >= MAX_QUEUE_DEPTH:
                return 0
            if not item.get("seq"):
                item = dict(item)
                item["seq"] = next(self._seq)
            self._items.append(item)
            return len(self._items)

    def drain(self, limit: int = 5) -> list[dict[str, Any]]:
        """原子取走队头若干条并清除。

        用「整体替换列表」而不是「for + pop」：后者在两方并发时，读和清之间
        到达的消息会被漏掉。
        """
        n = max(1, int(limit))
        with self._lock:
            taken = self._items[:n]
            self._items = self._items[n:]
        return taken

    def snapshot(self) -> list[dict[str, Any]]:
        """只读快照，给 UI 回显队里还有几条，不消费。"""
        with self._lock:
            return [dict(x) for x in self._items]

    def clear(self) -> int:
        with self._lock:
            n = len(self._items)
            self._items = []
            return n

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


# ---------------- 注册表 ----------------
# 键是 (user_id, session_id, generation_id)。刻意独立于 runtime 的
# generation 注册表实现，避免两处生命周期纠缠：这里只需要「取得到同一个对象」。
_queues: dict[tuple[str, str, str], SteerQueue] = {}
_registry_lock = threading.Lock()
# 每个 scope 的后台泵（Redis -> 本地）。多实例下必须有它，
# 否则在别的实例上入队的内容永远进不了本实例的队列。
_pumps: dict[tuple[str, str, str], Any] = {}


def _spawn_remote_push(scope, item: dict[str, Any]) -> None:
    """把一条插话发到 Redis（同步函数里的 fire-and-forget）。"""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    from .cluster import steerbus

    async def _go():
        try:
            await steerbus.push(*scope, item)
        except Exception:  # noqa: BLE001
            pass

    try:
        loop.create_task(_go())
    except Exception:  # noqa: BLE001
        pass


def _start_pump(key, queue: SteerQueue) -> None:
    """给这个 scope 起后台泵（幂等）。没配 Redis 时什么都不做。"""
    import asyncio

    try:
        from .cluster import steerbus
        if not steerbus.enabled():
            return
    except Exception:  # noqa: BLE001
        return
    if key in _pumps:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    pump = steerbus.SteerPump(queue, *key)
    pump.start()
    _pumps[key] = pump


def get_queue(user_id: str, session_id: str, generation_id: str) -> SteerQueue:
    key = (user_id or "", session_id or "", generation_id or "")
    with _registry_lock:
        q = _queues.get(key)
        if q is None:
            q = SteerQueue()
            q._scope = key
            _queues[key] = q
            created = True
        else:
            created = False
    if created:
        _start_pump(key, q)
    return q


def peek_queue(user_id: str, session_id: str,
               generation_id: str) -> SteerQueue | None:
    """只看有没有，不创建。用于「这条消息该排队还是该走普通发送」。"""
    with _registry_lock:
        return _queues.get((user_id or "", session_id or "", generation_id or ""))


def drop_queue(user_id: str, session_id: str, generation_id: str) -> int:
    key = (user_id or "", session_id or "", generation_id or "")
    with _registry_lock:
        q = _queues.pop(key, None)
        pump = _pumps.pop(key, None)
    if pump is not None:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(pump.stop())
        except RuntimeError:
            pass
    # 清掉 Redis 侧的残留，避免同 id 复用拿到上一轮的插话
    try:
        import asyncio

        loop = asyncio.get_running_loop()
        from .cluster import steerbus
        loop.create_task(steerbus.clear(*key))
    except (RuntimeError, Exception):  # noqa: BLE001
        pass
    return q.clear() if q is not None else 0


def queue_depth(user_id: str, session_id: str, generation_id: str) -> int:
    q = peek_queue(user_id, session_id, generation_id)
    return len(q) if q is not None else 0