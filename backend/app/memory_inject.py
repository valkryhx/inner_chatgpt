"""记忆读侧：每轮模型调用前把相关记忆注入上下文。

为什么走 ``message_injector``（attachment 通道）而不是塞 system prompt
------------------------------------------------------------------
系统的注入点已经现成：``agent_loop.py`` 的轮循环在每次模型调用前跑一次
``message_injector``（``agent_loop.py:353`` 附近），外面还包着
``InjectorGuard``（8s 超时、不丢消息）。记忆只需要接进这条管线 ——
不新建管线、不改 SSE 契约。

对照 CC：它把 ``MEMORY.md`` 从 system prompt 挪进 ``instructions`` attachment
（``m0848:1227-1228``，见 ``docs/20261114-CC319记忆机制源码复核v3.md`` §1.1），
理由正是「索引每轮都可能变，塞 system prompt 会让 prompt cache 全灭」。
Hlaudz 这里同理。

三条硬约束
----------
1. **组合，不覆盖**：``sse.py`` 用直接赋值装 steer 注入器，team 路径用
   ``subagent/session.py`` 的 ``_lead_injector``。直接再赋一次值会覆盖前面的。
   必须包一层 ``compose_injectors``，且**必须支持异步注入器** ——
   ``host.drain_pending`` 与 ``_lead_injector`` 都是协程，
   ``InjectorGuard.collect()`` 本来就按 ``inspect.isawaitable`` 分路。
   组合层若拒绝 awaitable，会表现为「记忆能注入、团队消息静默消失」。
2. **一个 generation 内同一条记忆只注入一次**：注入器挂在 while 循环里，
   一个用户轮次触发 10 次模型调用就会把同一批记忆 append 10 次，
   模型会把它当强调指令，行为变僵。去重键用 **slug 集合**而不是布尔标志
   （CC 用 ``readFileState`` 的路径集合，见 timing 文档 §5.1）——
   布尔标志会让「第三轮后台新写的 B」永远注入不进去。
3. **不进 ``session_history``**：注入块带 ``_volatile`` 标记，
   由 ``HistoryStore.set/append`` 统一剥离（记忆方案 §6.1）。
   这样每轮都是重新计算的最新记忆，compaction 也不会碰到它。
"""
from __future__ import annotations

import inspect
import logging
import os
from typing import Any, Callable, Iterable, Optional

from . import memory_store as ms

logger = logging.getLogger(__name__)

# 注入块的私有标记：HistoryStore 见到它就剥掉（不落 session_history）。
VOLATILE_KEY = "_volatile"

MEMORY_PREFIX = "[记忆]"


def memory_injection_enabled() -> bool:
    """总开关。``HLZ_MEMORY_ENABLED=0`` 时注入器直接返回空。"""
    return ms.memory_enabled()


def combine(fns: Iterable[Optional[Callable[[], Any]]]) -> Callable[[], Any]:
    """按顺序跑多个注入器，结果拼接。任一失败不影响其他。

    返回的是**协程函数**：注入点（``InjectorGuard.collect``）对 awaitable 走异步路，
    组合层统一成协程是最省心的形态 —— 同步注入器在里面直接调用即可，
    不需要调用方判断。

    为什么不能要求「只接受同步注入器」：``subagent/host.py`` 的
    ``drain_pending`` 和 team 的 ``_lead_injector`` 都是协程。
    拒绝 awaitable 会让团队/子代理路径的注入在组合后被当成异常吞掉，
    表现为「记忆能注入、团队消息静默消失」。
    """
    order = [fn for fn in fns if fn is not None]

    async def _combined() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for fn in order:
            try:
                got = fn()
                if inspect.isawaitable(got):
                    got = await got
            except Exception:  # noqa: BLE001 - 一个注入器失败不能拖垮整轮
                logger.warning("注入器失败，跳过：%s", getattr(fn, "__name__", fn),
                               exc_info=True)
                continue
            if not got:
                continue
            if isinstance(got, list):
                out.extend(m for m in got if isinstance(m, dict))
        return out

    return _combined


def wrap_with_memory(existing: Optional[Callable[[], Any]], memory_fn: Callable[[], Any],
                     *, memory_first: bool = True) -> Callable[[], Any]:
    """把记忆注入器包在现有注入器外面（不覆盖它）。

    顺序：``[记忆] -> [steer] -> [子代理通知] -> [team 收件箱]``。
    记忆放最前面，因为它是「背景知识」，应出现在用户新指令之前；
    后面的都是「本轮新发生的事」，靠近当前指令更符合注意力分布。
    """
    if existing is None:
        return memory_fn
    if memory_first:
        return combine((memory_fn, existing))
    return combine((existing, memory_fn))


class MemoryInjector:
    """记忆注入器：每轮挑出**还没注入过**的记忆，拼成一条 user 消息。

    为什么是类而不是函数：``_injected_slugs`` 需要跨模型调用存活，
    同时要跟「这是哪一次 generation」绑死。实例由
    ``install_memory_injector()`` 每次 ``run_agent_stream`` 新建一个，
    天然是 per-generation 的生命周期。
    """

    def __init__(self, user_id: str, *, scope: str = ms.SCOPE_PRIVATE,
                 query_provider: Optional[Callable[[], str]] = None) -> None:
        self.user_id = str(user_id)
        self.scope = scope
        # query_hint 用来做关键词加权（用户本轮说了什么）。由调用方提供，
        # 拿不到就退化成「按 kind 优先级 + 最近更新」。
        self._query_provider = query_provider
        self._injected_slugs: set[str] = set()
        self._last_query: str = ""

    def set_query_hint(self, text: str) -> None:
        self._last_query = (text or "").strip()

    def _query_hint(self) -> str:
        if self._query_provider is not None:
            try:
                return str(self._query_provider() or "")
            except Exception:  # noqa: BLE001
                return self._last_query
        return self._last_query

    async def __call__(self) -> list[dict[str, Any]]:
        if not memory_injection_enabled():
            return []
        try:
            candidates, extra_withheld = await ms.select_with_stats(
                self.user_id, query_hint=self._query_hint(), scope=self.scope
            )
        except Exception:  # noqa: BLE001 - 记忆读失败不能拖垮整轮
            logger.warning("记忆选择失败，本轮不注入", exc_info=True)
            return []
        fresh = [m for m in candidates if m.slug not in self._injected_slugs]
        if not fresh:
            return []
        # 扣留数 = 本轮因预算没进来的 + 之前已经注入过、这次被跳过的。
        # 后者也算「这次没显示出来」，否则用户在轮中途突然看不到某条会困惑。
        withheld = extra_withheld + (len(candidates) - len(fresh))
        text = ms.format_memory_block(fresh, withheld=withheld)
        if not text:
            return []
        self._injected_slugs.update(m.slug for m in fresh)
        try:
            await ms.touch_hits(self.user_id, [m.slug for m in fresh], scope=self.scope)
        except Exception:  # noqa: BLE001
            pass
        return [{
            "role": "user",
            "content": text,
            VOLATILE_KEY: True,
        }]


def rebuild_message_injector(loop) -> None:
    """按固定顺序重建 ``loop.message_injector``：记忆 -> steer -> 原始基线。

    为什么要有这个统一函数，而不是「谁装谁包一层」：
    ``loop`` 是跨 generation 复用的（``RuntimeHub`` 按 ``(user, model)`` 缓存），
    而**每个安装器各自记 baseline 会互相污染** —— 记忆注入器把「第一代的 steer
    注入器」记成 baseline，第二代重建时就又把它拼回去，插话从此失效。
    实测探针（2026-11-14）：gen1 能收到插话，gen2 空。

    正确做法是每个安装器只写自己的槽位，由这里按顺序从**当前**槽位重建：

        loop._memory_injector   ← 本代的 MemoryInjector
        loop._steer_injector    ← 本代的 steer 注入器
        loop._injector_base     ← 我们介入之前的原始注入器（只记一次）

    顺序按记忆方案 §3.3：记忆是背景知识，应出现在用户新指令之前；
    steer 是「刚刚补充的话」，放在最靠近当前消息的位置。
    """
    if not hasattr(loop, "_injector_base"):
        loop._injector_base = loop.message_injector
    parts: list[Callable[[], Any]] = []
    mem = getattr(loop, "_memory_injector", None)
    if mem is not None:
        parts.append(mem)
    steer = getattr(loop, "_steer_injector", None)
    if steer is not None:
        parts.append(steer)
    base = getattr(loop, "_injector_base", None)
    if base is not None:
        parts.append(base)
    if not parts:
        loop.message_injector = None
    elif len(parts) == 1:
        loop.message_injector = parts[0]
    else:
        loop.message_injector = combine(parts)


def install_memory_injector(loop, user_id: str, *,
                            query_hint: str = "",
                            scope: str = ms.SCOPE_PRIVATE) -> Optional[MemoryInjector]:
    """把记忆注入器装到 ``loop`` 上（包在现有注入器外面，不覆盖它）。

    返回注入器实例（调用方可以 ``set_query_hint`` 更新本轮的检索线索）。

    ⚠️ 必须在 ``run_stream`` 之前调用：``AgentLoop._injector_guard_now()``
    按 ``message_injector`` 的对象身份惰性绑定 guard，轮中途换注入器会丢掉
    进行中的后台 task（``agent_loop.py:160``）。

    ⚠️ ``_injected_slugs`` 的生命周期必须是 **per-generation**：
    ``loop`` 跨轮复用，所以这里每次都新建 ``MemoryInjector`` 并覆盖槽位，
    而不是往上一轮的组合结果上再包一层（那会让包装链无限增长，
    且每一层都做一次完整的记忆查询）。
    """
    if loop is None:
        return None
    if not memory_injection_enabled():
        return None
    injector = MemoryInjector(user_id, scope=scope)
    injector.set_query_hint(query_hint)
    try:
        loop._memory_injector = injector
        rebuild_message_injector(loop)
    except Exception:  # noqa: BLE001
        logger.warning("记忆注入器装配失败，本轮不注入", exc_info=True)
        return None
    return injector


def strip_volatile(history: Iterable[dict]) -> list[dict]:
    """剥掉带 ``_volatile`` 标记的消息（历史持久化前调用）。

    这是「记忆不进 ``session_history``」的**唯一执行点**，
    由 ``HistoryStore.set/append`` 调用 —— 那两个函数是 ``session_history``
    表的全部运行期写出口，所以在这里剥离能一次覆盖 agent_loop 的 3 处
    ``set()``、sse 的 1 处、compaction 的 3 处（记忆方案 §6.1）。
    """
    out: list[dict] = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        if msg.get(VOLATILE_KEY):
            continue
        if VOLATILE_KEY in msg:
            msg = {k: v for k, v in msg.items() if k != VOLATILE_KEY}
        out.append(msg)
    return out
