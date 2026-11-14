"""Self-built agent loop (replaces langchain create_agent + langgraph astream_events).

A plain async while-loop:
  model -> if tool_calls: dispatch each tool -> append tool results -> call model again
  -> until model returns no tool calls / max_turns / stopped.

Emits normalized events via async generator:
  {"type":"text_delta","text":...}
  {"type":"thinking_delta","text":...}
  {"type":"tool_start","id","name","input"}
  {"type":"tool_result","id","name","content","is_error"}
The SSE layer (sse.py) maps these + chart detection into the frontend protocol.

Design reference: GenericAgent genericagent/core/loop.py::agent_runner_loop.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from typing import Any, AsyncIterator, Optional

from .injector_guard import InjectorGuard, injector_timeout_from_env
from .llm_client import ChatResult, LLMClient, LLMError
from .tools import ToolRegistry

logger = logging.getLogger(__name__)


def _wall_clock_from_env(default: float = 0.0) -> float:
    """整轮墙钟上限（秒）。0 = 不限（保持历史行为）。

    为什么需要它：`max_turns` 只限制「模型往返次数」，不限制**时间**。
    一次往返可以合法地慢到几分钟（长思考模型 + 长输出 + 重试预算 180s），
    300 轮理论上能跑几小时。用户看到的就是「越跑越久，而且不知道还要多久」。

    为什么默认 0（不限）：这是兜底闸，不是常规手段。多数正常任务不该被它打断，
    贸然给个默认值会把「确实需要跑很久」的活切坏。需要硬上限时用
    `AGENT_WALL_CLOCK_S` 显式打开（如 1800 = 30 分钟）。
    """
    raw = (os.environ.get("AGENT_WALL_CLOCK_S") or "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


def _llm_retry_limit_from_env(default: int = 2) -> int:
    """模型调用彻底失败后允许的续跑次数。

    上限 3：续跑本身要重发整段上下文，次数多了只是把额度烧在一个坏掉的上游上。
    """
    raw = (os.environ.get("HLZ_AGENT_LLM_RETRIES") or "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    return max(0, min(n, 3))


def _friendly_llm_error(err: Exception) -> str:
    """把上游的原始报错翻译成用户能理解的一句话。

    上游 502 时原始错误是整段 nginx HTML（`<html><head><title>502 Bad
    Gateway</title>...`），直接展示对用户毫无意义，也看不出「这是上游的问题、
    不是你的操作有问题、已经自动重试过了」。
    """
    raw = str(err or "")
    head = raw.splitlines()[0] if raw else ""
    if "502" in head or "Bad Gateway" in raw:
        return ("上游模型服务暂时不可用（502 Bad Gateway）。已自动重试并续跑多次仍失败，"
                "这不是你的操作问题。稍后重发即可，已完成的工作都在上下文里。")
    if "504" in head or "Gateway Timeout" in raw:
        return ("上游模型服务响应超时（504 Gateway Timeout）。已自动重试仍失败，"
                "通常是上游负载过高，稍后重发即可。")
    if "429" in head or "rate limit" in raw.lower():
        return ("上游模型服务限流（429）。已自动退避重试仍失败，请稍后再试。")
    if "network error" in raw.lower():
        return ("与模型服务的连接中断，已自动重试仍失败。请检查网络或稍后重发。")
    # 其它情况保留原文，但截断，避免整段 HTML 砸到界面上
    return head[:300] if head else "模型调用失败"


def _team_grace_from_env(default: int = 3) -> int:
    """lead 想收工但团队还有活时，允许续跑的轮数。

    硬上限是必须的：没有它，「等队友」会变成永不结束的循环（队友可能一直在
    idle 待命）。默认 3 轮足够接住「等 reader 回来」这类典型场景。
    """
    raw = (os.environ.get("HLZ_LEAD_TEAM_GRACE_TURNS") or "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    return max(0, min(n, 10))


def _llm_retry_notice(event: str, attempt: int, err: Exception) -> None:
    """把续跑写进团队事件流，便于复盘"这次协作里断过几次、恢没恢复"。"""
    try:
        from .logging_setup import team_event
        team_event(event, attempt=attempt, error=str(err)[:300])
    except Exception:
        pass


def _describe_unknown_tool_result(tool_name: str) -> str:
    """崩溃中断后补进历史的「执行状态未知」说明。

    为什么这是模块级函数而不是 import `turn_marker.describe_unknown_result`：
    核心循环不 import 持久化模块（与 `on_turn_end` / `prefetch_factory` 同理）。
    文案必须与 `turn_marker.describe_unknown_result` 保持一致 —— 两处都有测试
    断言「未知」二字，改一处忘了另一处会被测出来。

    刻意**不**标成 is_error：它不是工具的失败，是观测的缺失。模型看到
    「错误」会倾向重试，看到「未知」才会先去看一眼产物。
    """
    name = tool_name or "该工具"
    return (
        f"[执行状态未知] {name} 在上一轮被进程中断，无法确定是否已执行完成。"
        f"它**可能已经产生了实际影响**（文件写入、命令执行、外部调用）。"
        f"重试前请先核对结果（例如读取目标文件、查看命令产物）；"
        f"若已生效，不要再执行一次。"
    )


def _dump(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


class AgentLoop:
    def __init__(self, client: LLMClient, registry: ToolRegistry, system_prompt: str,
                 max_turns: int = 300, history_store=None, *, user_id: str = "",
                 message_injector=None, team_gate=None, on_turn_end=None,
                 prefetch_factory=None):
        self.client = client
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.history_store = history_store  # optional; when set, persists history
        # 整轮墙钟上限（秒）；0 = 不限。见 _wall_clock_from_env 的说明。
        self.wall_clock_s = _wall_clock_from_env()
        # Owner identity is server-generated.  It is never accepted from a tool
        # argument or model output.
        self.user_id = user_id
        # 可选：每轮模型调用前的消息注入器（子代理 steer 用）
        self.message_injector = message_injector
        # 注入器超时预算（见 injector_guard）：0 = 不限（历史行为）。
        # 注意不能在这里就把 guard 绑死：`message_injector` 在构造之后会被
        # host.py / sse.py 重新赋值，所以必须按「当前注入器」惰性绑定。
        self._injector_timeout_s = injector_timeout_from_env()
        self._injector_guard = None
        self._injector_guard_for = None
        # 可选：团队待续跑闸。模型想收工时调用它；返回非空字符串表示
        # 「还有团队工作在跑」并给出原因，循环据此续跑而不是结束。
        # Team 之外永远是 None，行为与历史完全一致。
        self.team_gate = team_gate
        # 可选：轮次正常收尾时的侧边任务（记忆提取 / dream 等）。
        # 语义对齐 CC 的 throughStopHooks 阶段——那个阶段里并挂着五件互不相关的
        # 事，每件各自 try/except（m0848:3849-3876）。所以这里也是
        # **逐件独立**：一个失败不能连累下一个。
        #
        # 只在「正常完成」这一条出口触发，不在 stopped / error / 超墙钟时触发：
        # 半截对话提取出来的记忆往往是错的，而且用户已经明确中断了。
        self.on_turn_end = on_turn_end
        # 可选：记忆预取（M3.5）。只有「选择器开启 + 指定了独立小模型」时才非 None。
        # 形态见 `memory_prefetch.py`：模型调用前只**启动**，工具步骤后才尝试消费，
        # 这样选择器算得慢也不会挡住首 token。
        # 这里是回调而不是直接 import `memory_prefetch` —— 与 `on_turn_end` 同理：
        # 核心循环不该知道记忆模块存在，记忆关掉时也不在核心里留死代码。
        self.prefetch_factory = prefetch_factory
        # 本代的预取状态（slug 去重集合 / 调用计数 / 会话字节预算）。
        # `loop` 跨 generation 复用，所以**每代必须重置**，否则上一代的去重集合
        # 会让这一代的记忆永远注入不进来。
        self._memory_prefetch_state = None
        self._prefetch_handle = None
        # 崩溃上下文（持久化第二步）。None = 没有崩溃，`sanitize()` 维持
        # 既有行为（尾部悬空就重放）。非 None = 上一进程崩在这一轮，
        # **绝不重放**，改为补一条「执行状态未知」的结果让模型先核对。
        #
        # 为什么由外部注入而不让 AgentLoop 自己查库：核心循环不该知道
        # `turn_marker` 的存在（与 `on_turn_end` / `prefetch_factory` 同理），
        # 且这样测试可以精确构造场景。
        self._crash_context = None

    def set_crash_context(self, marker) -> None:
        """注入崩溃上下文；`None` 表示这一轮没有崩溃中断过。"""
        self._crash_context = marker

    def _injector_guard_now(self) -> InjectorGuard:
        """按**当前** `message_injector` 返回 guard（惰性绑定）。

        为什么不在 `__init__` 里建一次：`message_injector` 在构造之后会被
        `subagent/host.py` 与 `sse.py` 重新赋值。若 init 时就绑死，那两处赋值
        之后 guard 仍然包着旧的（可能是 None 的）注入器，等于注入静默失效。
        所以按注入器对象的身份缓存：换了注入器就换 guard。
        """
        if self._injector_guard is None or self._injector_guard_for is not self.message_injector:
            self._injector_guard = InjectorGuard(self.message_injector, self._injector_timeout_s)
            self._injector_guard_for = self.message_injector
        return self._injector_guard

    # ---------- sanitation: repair dangling tool_calls ----------
    async def sanitize(self, history: list[dict]) -> bool:
        """Repair tool-call protocol leftovers before a new model turn.

        A stopped turn can leave a ``role=tool`` message without its preceding
        assistant ``tool_calls`` message, or leave part of a multi-call block
        unanswered.  OpenAI-compatible gateways may report that malformed
        history as the misleading error "current model does not support
        function calling".  Remove orphan results and incomplete blocks; replay
        known missing tools only when the incomplete block is the tail.
        Returns True if ``history`` was changed (caller should persist it).
        """
        if not history:
            return False
        changed = False

        def call_ids(msg: dict) -> list[str]:
            return [
                str(tc.get("id"))
                for tc in (msg.get("tool_calls") or [])
                if tc.get("id")
            ]

        def drop_orphan_tool_messages() -> None:
            nonlocal changed
            known = {
                cid
                for msg in history
                if msg.get("role") == "assistant"
                for cid in call_ids(msg)
            }
            kept = [
                msg for msg in history
                if msg.get("role") != "tool" or str(msg.get("tool_call_id") or "") in known
            ]
            if len(kept) != len(history):
                history[:] = kept
                changed = True

        drop_orphan_tool_messages()
        i = 0
        while i < len(history):
            msg = history[i]
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                i += 1
                continue

            wanted = call_ids(msg)
            if not wanted:
                del history[i]
                changed = True
                continue

            j = i + 1
            while j < len(history) and history[j].get("role") == "tool":
                j += 1

            have = [str(history[k].get("tool_call_id") or "") for k in range(i + 1, j)]
            missing = [cid for cid in wanted if cid not in have]
            if not missing:
                # Keep exactly one result for each call.  Duplicate results are
                # also invalid even when the call id itself is known.
                seen: set[str] = set()
                block: list[dict] = []
                for k in range(i + 1, j):
                    cid = str(history[k].get("tool_call_id") or "")
                    if cid in wanted and cid not in seen:
                        block.append(history[k])
                        seen.add(cid)
                if len(block) != j - i - 1:
                    history[i + 1:j] = block
                    changed = True
                    i += 1 + len(block)
                else:
                    i = j
                continue

            has_later_non_tool = any(m.get("role") != "tool" for m in history[j:])
            names = set(self.registry.names()) if self.registry is not None else set()
            can_replay = all(
                (tc.get("function") or {}).get("name") in names
                for tc in (msg.get("tool_calls") or [])
                if str(tc.get("id") or "") in missing
            )
            if not has_later_non_tool and can_replay:
                # ⚠️ 崩溃中断时**不能重放**：那个工具可能已经执行过。
                # `_crash_context` 非 None 说明上一进程没走完这一轮就死了，
                # 我们原理上无法确定它执行到哪一步。
                #
                # 这里补一条**明确的「执行状态未知」**结果，而不是静默重放：
                # 让模型据此决定要不要重试、要不要先核对产物。伪装成失败
                # 会诱发盲目重试，伪装成成功会丢失工作 —— 只有如实说
                # 「不知道」才是正确的。
                if self._crash_context is not None:
                    for tc in msg.get("tool_calls") or []:
                        tid = str(tc.get("id") or "")
                        if tid not in missing:
                            continue
                        name = (tc.get("function") or {}).get("name") or ""
                        history.append({
                            "role": "tool",
                            "tool_call_id": tid,
                            "name": name,
                            "content": _describe_unknown_tool_result(name),
                        })
                    changed = True
                    i = len(history)
                    continue
                for tc in msg.get("tool_calls") or []:
                    tid = str(tc.get("id") or "")
                    if tid not in missing:
                        continue
                    name = (tc.get("function") or {}).get("name")
                    args = _safe_json((tc.get("function") or {}).get("arguments"))
                    out = await self.registry.dispatch(name, args)
                    history.append({
                        "role": "tool",
                        "tool_call_id": tid,
                        "name": name,
                        "content": out,
                    })
                changed = True
                i = len(history)
                continue

            # Keep neither half of an incomplete exchange.  Leaving the tool
            # result behind is what makes the next request invalid.
            del history[i:j]
            changed = True
            # Inspect the item that shifted into this index.

        # A dropped incomplete assistant block may have left a result whose
        # call was removed above.
        drop_orphan_tool_messages()
        return changed

    # ---------- main loop ----------
    async def run_stream(
        self,
        history: list[dict],
        stop_event: Optional[asyncio.Event] = None,
        session_id: str = "",
    ) -> AsyncIterator[dict]:
        """公开入口：包一层，保证任何出口（含异常/取消）都收尾注入器。

        为什么要包装而不是在循环里加 flush：`_run_stream_inner` 有 5 个
        `return` 出口（停止/错误/完成/超墙钟/超轮数）外加异常与取消路径，
        逐个加必然漏掉一个。放在生成器的 finally 里只写一次。
        """
        try:
            async for ev in self._run_stream_inner(history, stop_event, session_id):
                yield ev
        finally:
            # 预取可能还在飞（本轮没赶上消费点）。不取消会让它挂到超时，
            # 而且下一轮 `_start_prefetch` 覆盖句柄后它就成了孤儿 task。
            self._cancel_prefetch()
            await self._flush_injector()

    async def _flush_injector(self) -> None:
        """给未完成的注入任务一个短宽限；真丢时留明确日志（见 injector_guard）。"""
        guard = self._injector_guard
        if guard is None:
            return
        try:
            await guard.flush()
        except BaseException:  # noqa: BLE001 - 收尾绝不能影响正常结束
            logger.warning("注入器收尾失败", exc_info=True)

    # ---------- 记忆预取（M3.5）：启动与消费 ----------
    #
    # 形态对齐 CC 的两个挂点（`m0848:7294` 启动 / `:10602` 消费）：
    # 模型调用之前只**启动**，工具步骤之后才尝试**消费**。
    # 为什么不能合到一个点：合在「模型调用前」就退化成同步等待（选择器
    # 300-2000ms 会直接进 TTFT）；合在「工具步骤后」则第一轮永远拿不到。
    #
    # 这里只是转发到 `prefetch_factory`，核心循环不 import 记忆模块 ——
    # 与 `on_turn_end` 同样的理由：记忆关掉时核心里不该留死代码。

    def _start_prefetch(self, history: list[dict]) -> None:
        factory = self.prefetch_factory
        if factory is None:
            return
        try:
            self._prefetch_handle = factory(self, history)
        except Exception:  # noqa: BLE001 - 预取是增强，启动失败不该影响本轮
            logger.warning("记忆预取启动失败，本轮走关键词路径", exc_info=True)
            self._prefetch_handle = None

    async def _take_prefetch(self, turn: int) -> list[dict]:
        handle = self._prefetch_handle
        if handle is None:
            return []
        try:
            return await handle.take(turn)
        except Exception:  # noqa: BLE001
            logger.warning("记忆预取消费失败，本轮不注入", exc_info=True)
            return []

    def _cancel_prefetch(self) -> None:
        handle = self._prefetch_handle
        self._prefetch_handle = None
        if handle is None:
            return
        try:
            handle.cancel()
        except Exception:  # noqa: BLE001
            pass

    async def _run_turn_end_hooks(self, history: list[dict]) -> None:
        """轮次正常完成时跑侧边任务。**逐件独立**，一件失败不影响其他。

        为什么逐件独立：CC 在同一个「轮次收尾」阶段里挂了五件互不相关的事
        （post-turn summary / 记忆提取 / dream / prompt suggestion / brief 检查，
        `m0848:3849-3876`），每件各自 try/catch。摘要失败不该拖累记忆提取。
        做成「一个失败全跳过」是这类设计里最常见的错。

        为什么只在正常完成时跑：stopped / error / 超墙钟都是「这一轮没跑完」，
        半截对话提取出来的记忆往往是错的，而且用户已经明确中断了 ——
        他不需要系统在背后继续消化他的中断。
        """
        hook = self.on_turn_end
        if hook is None:
            return
        try:
            result = hook(history)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - 收尾失败绝不能影响本轮成败
            logger.warning("轮次收尾任务失败（不影响本轮结果）", exc_info=True)

    async def _run_stream_inner(
        self,
        history: list[dict],
        stop_event: Optional[asyncio.Event] = None,
        session_id: str = "",
    ) -> AsyncIterator[dict]:
        """Mutates `history` in place (appends assistant/tool messages). Emits events.
        The caller is responsible for appending the trailing user message first."""
        def stopped() -> bool:
            return bool(stop_event is not None and stop_event.is_set())

        # repair any dangling state before a new turn
        try:
            changed = await self.sanitize(history)
            if changed and self.history_store is not None:
                if self.user_id:
                    await self.history_store.set(self.user_id, session_id, history)
                else:
                    await self.history_store.set(session_id, history)
        except Exception:
            pass

        turn = 0
        # 模型调用彻底失败后的续跑计数（与 turn 分开：续跑不应该吃掉正常轮数预算）
        llm_retries = 0
        llm_retry_limit = _llm_retry_limit_from_env()
        # 团队待续跑预算：lead 想收工但队友还在跑时允许续几轮。
        team_grace_left = _team_grace_from_env()
        # 墙钟起点：max_turns 限次数，这一条限时间。0 = 不限。
        started_at = time.monotonic()
        # 记忆预取（M3.5）：**在首轮模型调用之前只启动，不等待**。
        # 与下面的「工具步骤之后消费」配对，才是 CC 的完整语义（m0848:7294/:10602）。
        self._start_prefetch(history)
        while turn < self.max_turns:
            if stopped():
                yield {"type": "done", "status": "stopped"}
                return
            # 墙钟闸：放在每轮开头，保证超时后不会先发起下一次模型调用。
            if self.wall_clock_s > 0:
                elapsed = time.monotonic() - started_at
                if elapsed > self.wall_clock_s:
                    logger.warning("整轮墙钟超限：%.0fs > %.0fs（第 %s 轮），停止",
                                   elapsed, self.wall_clock_s, turn)
                    yield {"type": "error",
                           "error": (f"本轮运行已超过 {self.wall_clock_s:.0f}s 上限，已停止"
                                     f"（第 {turn} 轮）。可用 AGENT_WALL_CLOCK_S 调整，"
                                     "或设为 0 取消该上限。")}
                    return
            turn += 1
            if self.message_injector is not None:
                guard = self._injector_guard_now()
                injected, inject_timed_out = await guard.collect()
                if inject_timed_out:
                    # 只发事件、不中断本轮：注入是增强，失败不该让整轮失败。
                    yield {"type": "injector_timeout",
                           "timeout_s": self._injector_timeout_s}
                for msg in injected:
                    history.append(msg)
                    yield {"type": "message_injected", "text": msg.get("content", "")}
            # streaming deltas must be yielded immediately; run() collects them.
            # We use a queue to bridge on_delta callback -> async yields.
            q: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def on_delta_q(kind: str, text: str) -> None:
                loop.call_soon_threadsafe(q.put_nowait, (kind, text))

            async def _drive():
                try:
                    # 图片只在「送模型的那一份」里展开成 data URL：
                    # history 本身要保持纯文本，否则 base64 会被持久化进 session_history。
                    wire = history
                    if self.user_id:
                        try:
                            from .uploads import expand_images_for_wire
                            wire = expand_images_for_wire(history, self.user_id)
                        except Exception:
                            wire = history
                    res = await self.client.run(wire, self.registry.schemas(),
                                                on_delta=on_delta_q, stop_check=stopped)
                    return res
                finally:
                    loop.call_soon_threadsafe(q.put_nowait, ("__end__", None))

            drive_task = asyncio.create_task(_drive())
            result: Optional[ChatResult] = None
            try:
                while True:
                    kind, text = await q.get()
                    if kind == "__end__":
                        break
                    if kind == "reasoning":
                        yield {"type": "thinking_delta", "text": text}
                    else:
                        yield {"type": "text_delta", "text": text}
                result = await drive_task
            except LLMError as e:
                # 调用层（llm_client）已经把网络级重试用完才抛到这里。到这一层
                # 说明该模型调用彻底失败。直接结束整轮对单 agent 是"如实报错"，
                # 对 team 是灾难：lead 一死，它派的队友还在跑、任务板还有
                # pending，用户拿不到任何结论。真机 runId 111915-64a6 就是这么
                # 僵死 927 秒的。
                #
                # 续跑只重发**模型调用**，不重跑任何工具：history 原样保留，
                # 模型看到自己已完成的工具结果后继续。因此重试是幂等的。
                if (not stopped()) and llm_retries < llm_retry_limit:
                    llm_retries += 1
                    logger.warning("模型调用失败，续跑（%s/%s）：%s",
                                   llm_retries, llm_retry_limit, str(e)[:300])
                    yield {"type": "llm_retry", "attempt": llm_retries,
                           "limit": llm_retry_limit, "error": str(e)}
                    # 给模型一句可执行的话，而不是把整段异常塞进上下文。
                    history.append({
                        "role": "user",
                        "content": ("[平台提示] 上一次模型调用因接口中断失败了，"
                                    "这不是你的错误，也不代表任务无法继续。"
                                    "请从中断处继续完成当前任务：已执行过的工具结果都在上下文里，"
                                    "不要重复执行它们，直接往下做。"),
                    })
                    _llm_retry_notice("agent_loop_continue", llm_retries, e)
                    continue
                _llm_retry_notice("agent_loop_gave_up", llm_retries, e)
                yield {"type": "error", "error": _friendly_llm_error(e)}
                return
            except asyncio.CancelledError:
                # Cancelling the outer SSE task must also stop the inner LLM
                # request.  Otherwise the model call keeps running in the
                # background and can mutate history after the user stopped.
                if not drive_task.done():
                    drive_task.cancel()
                try:
                    await drive_task
                except BaseException:
                    pass
                raise
            finally:
                if not drive_task.done():
                    drive_task.cancel()

            if stopped():
                yield {"type": "done", "status": "stopped"}
                return

            content = result.content if result else ""
            reasoning = result.reasoning if result else ""
            tool_calls = result.tool_calls if result else []

            if not tool_calls:
                history.append({"role": "assistant", "content": content,
                                **({"reasoning": reasoning} if reasoning else {})})
                if self.history_store is not None:
                    if self.user_id:
                        await self.history_store.set(self.user_id, session_id, history)
                    else:
                        await self.history_store.set(session_id, history)
                # 团队待续跑闸：lead 说「等 reader」之后模型可能就结束了这一轮，
                # 而队友还在跑、结果还没回来。此时收工 = 队友的结论永远没人接
                # （真机 runId 111915-64a6 里任务 3 就是这样永久 pending 的）。
                # 只有确认「确实还有活着的队友 / 未消费的消息」才续跑，
                # 并且有硬上限，队友全部结束后立即收工。
                if (not stopped()) and team_grace_left > 0 and self.team_gate is not None:
                    try:
                        pending = self.team_gate()
                        if inspect.isawaitable(pending):
                            pending = await pending
                    except Exception:  # noqa: BLE001 - 闸坏了不能卡住对话
                        pending = None
                    if pending:
                        team_grace_left -= 1
                        mine = team_grace_left
                        _llm_retry_notice("lead_wait_team", mine, Exception(str(pending)[:200]))
                        history.append({
                            "role": "user",
                            "content": f"[平台提示] {pending}\n"
                                       "不要就此结束：等它们的结果回来，然后用它们"
                                       "完成任务、更新任务板，最后再给用户一个完整结论。",
                        })
                        yield {"type": "team_wait", "reason": str(pending),
                               "grace_left": mine}
                        continue
                # 轮次正常收尾：跑侧边任务（记忆提取 / dream）。
                # 必须在 yield done 之前跑完 —— 之后 SSE 就关了，后台任务
                # 拿不到「本轮发生了什么」。这些函数内部各自判闸门，
                # 不满足立即返回；异常各自吞掉，绝不影响本轮成败。
                await self._run_turn_end_hooks(history)
                yield {"type": "done", "status": "complete"}
                return

            # append assistant(tool_calls) + run each tool
            amsg = {"role": "assistant", "content": content, "tool_calls": tool_calls}
            if reasoning:
                amsg["reasoning"] = reasoning
            history.append(amsg)
            prepared = []
            for tc in tool_calls:
                if stopped():
                    break
                fid = tc.get("id") or ""
                name = tc.get("function", {}).get("name", "")
                args = _safe_json(tc.get("function", {}).get("arguments"))
                prepared.append({"id": fid, "name": name, "args": args})
                yield {"type": "tool_start", "id": fid, "name": name, "input": args}
            if prepared and not stopped():
                # 相邻的只读工具并发，写类与 code_run 串行（tool_batching 保证顺序）
                from .tool_batching import run_batched

                outs = await run_batched(prepared, self.registry.dispatch)
                for item, out in zip(prepared, outs):
                    is_err = out.startswith("[工具执行错误]")
                    history.append({"role": "tool", "tool_call_id": item["id"],
                                    "name": item["name"], "content": out})
                    yield {"type": "tool_result", "id": item["id"], "name": item["name"],
                           "content": out, "is_error": is_err}
                    if self.history_store is not None:
                        if self.user_id:
                            await self.history_store.set(self.user_id, session_id, history)
                        else:
                            await self.history_store.set(session_id, history)

            # 记忆预取的消费点：**工具步骤之后**（对齐 CC 的 m0848:10602）。
            # 三个条件缺一不可：已 settle、还没消费过、在工具步骤之后。
            # 若选择器此刻还没算完，这一轮就不注入 —— 这正是「不拖慢首 token」
            # 的实现方式：慢的预取自动让位，留到下一轮再看。
            for msg in await self._take_prefetch(turn):
                history.append(msg)
                yield {"type": "message_injected", "text": msg.get("content", "")}

        yield {"type": "error", "error": f"达到最大轮数 {self.max_turns}，已停止"}


def _safe_json(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_raw": raw}
    except Exception:
        return {"_raw": raw}
