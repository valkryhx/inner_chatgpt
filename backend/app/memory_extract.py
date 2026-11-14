"""M4 后半：把「该不该记」的判定接到真的后台提取子代理上。

分工（与方案 §4.1 / §4.2 / §4.3 对齐）::

    memory_write.py   纯函数：四道闸门 + 提示词（无副作用，可单测）
    memory_extract.py 本模块：读 history -> 判闸 -> 派子代理 -> 收尾登记
    agent_loop.py     只提供 on_turn_end 钩子，不 import 记忆模块

为什么提取**不**复用 ``SubagentHost.spawn``
------------------------------------------
``spawn`` 是给「模型主动派活」用的，它有四个副作用，每一个都不该出现在后台提取上：

1. 抢 ``subagent_lease`` 配额 —— 提取会挤掉用户真正派的子代理；
2. 发 ``subagent_start`` / ``subagent_end`` 事件 —— 前端会弹出一张卡片，
   用户看到「Agent 正在整理记忆」，而他根本没要求这件事；
3. 写 ``subagent_runs`` 行 —— 侧链表被大量「记忆提取」run 撑满；
4. 结果进 ``read_agent_result`` 可读集合 —— 模型能读到自己的提取过程，
   下一次提取就会看到上一次的提取结论，形成反馈回路。

所以这里**直接构造一个一次性的 AgentLoop**：与子代理同构（同一个 client、
同一个墙钟、同样的 max_turns 语义），但不进 registry、不占配额、不发事件。

闭世界工具集（方案 §4.3）
-------------------------
写入子代理**只有** ``memory_write`` 一个工具。CC 的 ``z$t`` 写侧子集里还有
``memory_str_replace`` / ``memory_append``，Hlaudz 第一版没有这两个工具
（对外只承诺 ``Nji`` 三件套，见 §5），所以这里只给 ``memory_write``。
**拿不到 code_run / file_write / MCP** —— prompt 不是安全边界，工具 schema 才是。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import memory_store as ms
from . import memory_write as mw
from .agent_loop import AgentLoop
from .llm_client import ChatResult
from .memory_tools import build_memory_tools
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

# 提取子代理的轮数上限。它只需要「看一眼 -> 写几条」，3 轮足够；
# 给多了会让一次提取烧掉几十秒。
DEFAULT_EXTRACT_MAX_TURNS = 4

# 整次提取的墙钟上限（秒）。后台任务必须有硬上限 —— 否则上游卡住时
# 这个 task 会一直挂着，用户每轮都在叠加新的。
DEFAULT_EXTRACT_TIMEOUT_S = 90.0

# 单次提取最多派几个（同时）——本方案串行，这里只为将来留位置。
_MAX_CONCURRENT = 1


def _env_int(name: str, default: int, *, lo: int = 1) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, int(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, *, lo: float = 1.0) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, float(raw))
    except (TypeError, ValueError):
        return default


def extract_timeout_s() -> float:
    return _env_float("HLZ_MEMORY_EXTRACT_TIMEOUT_S", DEFAULT_EXTRACT_TIMEOUT_S)


def extract_max_turns() -> int:
    return _env_int("HLZ_MEMORY_EXTRACT_MAX_TURNS", DEFAULT_EXTRACT_MAX_TURNS)


@dataclass
class ExtractOutcome:
    """一次提取尝试的结果。用于日志、审计与测试断言。"""

    ran: bool = False
    reason: str = ""
    written: list[str] = field(default_factory=list)
    turns: int = 0
    error: str = ""
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran, "reason": self.reason,
            "written": list(self.written), "turns": self.turns,
            "error": self.error, "elapsed_s": round(self.elapsed_s, 2),
        }


class _WriteRecorder:
    """包一层 ``memory_write`` 工具，记下这次提取到底写了什么。

    为什么要记录：写完要说得出「记了什么」。没有这个，出了问题只能去翻文件
    和模型日志猜。也用于「写完之后的通知注入」（方案 §4.4）。
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.written: list[str] = []

    def wrap(self, tools: list) -> list:
        out = []
        for tool in tools:
            if tool.name != "memory_write":
                out.append(tool)
                continue
            original = tool.call

            async def _call(args: dict, _orig=original) -> str:
                result = await _orig(args)
                # 只记「真的写成功了」。冲突/拒绝不算 —— 那没产生新记忆。
                try:
                    import json
                    payload = json.loads(result)
                    if payload.get("outcome") == "ok":
                        slug = str(payload.get("slug") or "")
                        if slug:
                            self.written.append(slug)
                except Exception:  # noqa: BLE001 - 解析失败不影响工具本身
                    pass
                return result

            out.append(type(tool)(
                name=tool.name, description=tool.description,
                input_schema=tool.input_schema, call=_call, source=tool.source,
            ))
        return out


def build_extract_registry(user_id: str, recorder: _WriteRecorder) -> ToolRegistry:
    """提取子代理的闭世界工具集：**只有** ``memory_write``。

    注意这里**不是** ``build_builtin_tools(include_memory=False)`` 的反面 ——
    那个给的是 root 的完整记忆三件套（含 list/read）。提取器不需要 list/read：
    提示词里已经给了它「先看已有的」这件事怎么做，用的是提示词上下文而不是工具
    （第一版简化）。给多一个工具就多一个被注入内容诱导的面。
    """
    tools = [t for t in build_memory_tools(user_id) if t.name == "memory_write"]
    tools = recorder.wrap(tools)
    return ToolRegistry(tools)


EXTRACT_SYSTEM_PROMPT = (
    "你是记忆提取子代理。你唯一的工具是 memory_write。\n"
    "不要寒暄、不要解释你的判断过程，只调用工具；没有值得记的就直接结束。\n"
    "严格遵循下面的规则。"
)


async def run_extraction(client, user_id: str, conversation: str, *,
                         timeout_s: float = 0.0,
                         max_turns: int = 0) -> ExtractOutcome:
    """真正跑一次提取。**不判闸门** —— 闸门由调用方（``maybe_extract``）判。

    单独暴露是为了让测试能绕过闸门直接验证「提取本身能不能work」。
    """
    started = time.monotonic()
    out = ExtractOutcome(ran=True)
    recorder = _WriteRecorder(user_id)
    registry = build_extract_registry(user_id, recorder)

    prompt = mw.build_extract_prompt(conversation)
    loop = AgentLoop(
        client, registry, EXTRACT_SYSTEM_PROMPT,
        max_turns=max_turns or extract_max_turns(),
        # 不挂 history_store：提取器的对话不该进 session_history，
        # 也不该被用户看到（它不是这次对话的一部分）。
        history_store=None,
        user_id=user_id,
    )
    history: list[dict] = [{"role": "user", "content": prompt}]

    async def _drive() -> None:
        async for ev in loop.run_stream(history):
            if ev.get("type") == "error":
                out.error = str(ev.get("error") or "")
            elif ev.get("type") == "tool_start":
                out.turns += 1

    budget = timeout_s or extract_timeout_s()
    try:
        await asyncio.wait_for(_drive(), timeout=budget)
    except asyncio.TimeoutError:
        out.error = f"超过 {budget:.0f}s 未完成"
        logger.warning("[memory] 提取超时（%.0fs），本轮放弃", budget)
    except Exception as exc:  # noqa: BLE001 - 提取失败不能影响主对话
        out.error = f"{type(exc).__name__}: {exc}"
        logger.warning("[memory] 提取失败", exc_info=True)
    finally:
        out.written = list(recorder.written)
        out.elapsed_s = time.monotonic() - started
    return out


def _turn_counter(user_id: str) -> "_TurnCounter":
    """每个用户一个轮次计数器（进程内，重启归零）。"""
    return _counters.setdefault(user_id, _TurnCounter())


class _TurnCounter:
    __slots__ = ("n",)

    def __init__(self) -> None:
        self.n = 0

    def tick(self) -> int:
        self.n += 1
        return self.n


_counters: dict[str, _TurnCounter] = {}


def reset_turn_counter(user_id: str = "") -> None:
    """测试与排障用。"""
    if user_id:
        _counters.pop(user_id, None)
    else:
        _counters.clear()


async def maybe_extract(ur, history: list[dict], *, client=None,
                        turn_count: int = 0) -> ExtractOutcome:
    """**同步**版提取：判四道闸，通过就当场跑完并返回结果。

    生产路径**不用**它（那会让 SSE 卡在结尾，见 ``make_turn_end_hook``）；
    它存在的价值是：测试与排障可以「确定性地跑一次提取并拿到结果」，
    不用等后台 task、也不用 sleep。

    ``HLZ_MEMORY_DISABLED=1`` / ``HLZ_MEMORY_AUTO_WRITE=0`` 时直接返回，
    不消耗轮次计数 —— 否则关掉再打开会「补跑」一堆本该跳过的轮次。
    """
    user_id = str(getattr(ur, "user_id", "") or "")
    if not user_id:
        return ExtractOutcome(ran=False, reason="没有 user_id")

    # 先看总开关，再动计数器（见 docstring）
    if ms.memory_hard_disabled() or not ms.auto_write_enabled():
        decision = mw.evaluate_gates([], turn_count=0)
        return ExtractOutcome(ran=False, reason=decision.reason)

    turn_count = turn_count or _turn_counter(user_id).tick()
    decision = mw.evaluate_gates(
        history, turn_count=turn_count,
        every_n=mw.every_n_turns(), min_words=mw.min_prose_words(),
        # 闸 4：现有权限体系里没有「逐文件写规则」，等价物是「这个用户能不能写」。
        # 恒 True —— 记忆目录是我们自己的沙箱，没有理由拒绝。
        permission_ok=True,
    )
    if not decision.allowed:
        return ExtractOutcome(ran=False, reason=decision.reason)

    conversation = mw.conversation_from(history)
    if not conversation.strip():
        return ExtractOutcome(ran=False, reason="窗口内没有可读对话")

    llm = client or getattr(getattr(ur, "model", None), "client", None)
    if llm is None:
        return ExtractOutcome(ran=False, reason="没有可用的 LLM client")

    out = await run_extraction(llm, user_id, conversation)
    if out.written:
        logger.info("[memory] 提取写入 %d 条：%s", len(out.written), out.written)
    else:
        logger.debug("[memory] 提取未写入任何条目（reason=%s）",
                     out.error or "模型判断无值得记的")
    return out


def make_turn_end_hook(ur):
    """构造给 ``AgentLoop(on_turn_end=...)`` 用的回调。

    **必须 fire-and-forget**：``on_turn_end`` 在 ``yield done`` 之前被 await，
    如果在这里同步等提取跑完，一次最长 90 秒的提取会让 SSE 流卡在结尾 ——
    用户看到的是「每轮结束都要等几十秒」，这比不提取糟得多。
    所以钩子只负责**起一个后台 task 并立刻返回**。

    后台 task 的约束：

    - **不随本轮取消**：用户点停止、SSE 断开都不该打断已经开始的提取
      （它只写记忆，不产生用户可见的输出）；
    - **history 要快照**：循环结束后 history 可能被下一轮继续 append，
      直接传引用会让提取器读到未来的消息；
    - **限并发**：同一用户最多一个提取在跑。叠加只会浪费钱，而且两个提取器
      同时写同一批 slug 会互相冲突。
    """
    user_id = str(getattr(ur, "user_id", "") or "")
    client = getattr(getattr(ur, "model", None), "client", None)

    async def _hook(history: list[dict]) -> None:
        if _inflight.get(user_id):
            logger.debug("[memory] 该用户上一次提取还没结束，本轮跳过")
            return
        # 快照：只要 role/content/tool_calls 这类字段，深拷贝一层
        snapshot = [dict(m) for m in history if isinstance(m, dict)]
        task = asyncio.create_task(_run_detached(user_id, client, snapshot))
        _inflight[user_id] = task
        _background.add(task)
        task.add_done_callback(_background.discard)

    return _hook


# 正在跑的提取（每用户一个）；也兼作「限并发」的锁
_inflight: dict[str, asyncio.Task] = {}
# 保持强引用，避免 task 被 GC 回收（asyncio 只持弱引用）
_background: set[asyncio.Task] = set()


async def _run_detached(user_id: str, client, history: list[dict]) -> None:
    """后台提取的实际执行体。任何异常都只记日志，绝不外抛。"""
    try:
        if ms.memory_hard_disabled() or not ms.auto_write_enabled():
            return
        if client is None:
            logger.debug("[memory] 没有可用 client，跳过提取")
            return
        turn_count = _turn_counter(user_id).tick()
        decision = mw.evaluate_gates(
            history, turn_count=turn_count,
            every_n=mw.every_n_turns(), min_words=mw.min_prose_words(),
            permission_ok=True,
        )
        if not decision.allowed:
            logger.debug("[memory] 闸门未通过：%s", decision.reason)
            return
        conversation = mw.conversation_from(history)
        if not conversation.strip():
            return
        out = await run_extraction(client, user_id, conversation)
        if out.written:
            logger.info("[memory] 后台提取写入 %d 条：%s", len(out.written), out.written)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 后台提取失败绝不能影响任何东西
        logger.warning("[memory] 后台提取失败（已忽略）", exc_info=True)
    finally:
        _inflight.pop(user_id, None)


async def drain_background(timeout_s: float = 5.0) -> None:
    """等所有在跑的后台提取结束。**只给测试与进程收尾用**。

    生产路径不等 —— 那正是「fire-and-forget」的意义。但测试需要它，
    否则断言会在 task 还没跑完时就执行，结果不稳定。
    """
    tasks = [t for t in _background if not t.done()]
    if not tasks:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
                               timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.debug("[memory] 后台提取在 %.0fs 内未全部结束（忽略）", timeout_s)
