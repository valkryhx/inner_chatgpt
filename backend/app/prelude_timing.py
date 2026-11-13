"""首帧前奏的分段计时（可观测性基础设施）。

为什么需要这个模块
------------------
`_event_stream` 在发出 `message_start` 之前要串行做 7 件事（写消息行、建 runtime、
软/硬线压缩、sanitize、派生 turn runtime、prepare team）。其中
`enforce_soft_limit` 内部会调一次 LLM 生成摘要，在超软线的会话里实测会把首帧
推迟 5 秒以上（见 docs/20261112-Hlaudz首帧串行阻塞核实与优化设计.md §1.1）。

问题不是「慢」本身，而是**慢在哪一段完全看不见**：线上只会看到「今天好像变卡了」，
没有任何字段能定位。这个模块把前奏切成有名字的段，最后汇总成**一行结构化日志**：

    chat.prelude user=u_xxx session=co_xxx gen=g_x turns=1 total_ms=241
        add_msg=12 runtime=8 sanitize=3 soft=0 soft_called=0 hard=1
        build_turn=214 team=2 first_frame=241 deferred=0

这样「首帧为什么慢」从猜测变成一个可以 grep 的数字。

设计取舍
--------
- **只在 `extra=` 里放字段**，消息体保持一句人可读的话：`logging_setup` 的文件
  handler 用的是普通 Formatter（不吃 extra），把它当纯文本看也不会变成一坨 JSON。
- **异常也要能计时**：某一段抛异常时记录 `exc` 字段并继续，不能因为埋点本身
  影响请求成败（埋点绝不能成为新的故障点）。
- **不引入依赖、不建全局状态**：每个请求一个新的 `PreludeTimer`，随请求消失。
- **默认零成本关闭**：`HLZ_PRELUDE_LOG=0` 时不产生任何日志，计时本身也只做
  几个 `time.monotonic()` 调用（微秒级）。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from .logging_setup import PRELUDE_LOGGER_NAME

# 走专用通道：普通 root Formatter 不渲染 `extra`，各段耗时字段会全部丢失
# （2026-11-12 实测：只有 total 落进 app.log，`soft=5000` 之类的字段一个都没出来）。
# 专用通道挂 JsonLineFormatter，字段完整落盘。
logger = logging.getLogger(PRELUDE_LOGGER_NAME)


def prelude_log_enabled() -> bool:
    """是否输出前奏汇总日志。默认开。

    P1 阶段这条日志是唯一的验收依据，所以默认开；等首帧指标稳定后，
    高频部署可以关掉（`HLZ_PRELUDE_LOG=0`）。
    """
    raw = (os.environ.get("HLZ_PRELUDE_LOG") or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


class PreludeTimer:
    """按名字累计前奏各段耗时，最后一行汇总。

    用法：

        t = PreludeTimer(user_id, session_id, generation_id)
        async with t.span("add_msg"):
            await db.add_message(...)
        ...
        t.mark("first_frame")       # 记录一个时刻（不是一段）
        t.emit()                    # 输出一行汇总

    设计上刻意用 `span()` 包住而不是 `start/end` 成对调用：成对调用的常见
    事故是异常路径漏掉 `end`，导致数字永远偏小且没人发现。
    """

    __slots__ = ("user_id", "session_id", "generation_id", "_t0", "_spans",
                 "_marks", "_counts", "_exc")

    def __init__(self, user_id: str = "", session_id: str = "",
                 generation_id: str = "") -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.generation_id = generation_id
        self._t0 = time.monotonic()
        self._spans: dict[str, float] = {}
        self._marks: dict[str, float] = {}
        self._counts: dict[str, int] = {}
        self._exc: dict[str, str] = {}

    # ---------- 计时 ----------

    def span(self, name: str) -> "_Span":
        """返回一个同步上下文管理器，包住的耗时累计到 `name` 名下。"""
        return _Span(self, name)

    def add(self, name: str, ms: float) -> None:
        """外部已测好的耗时（例如 runtime 内部自己算完回报）。"""
        self._spans[name] = self._spans.get(name, 0.0) + ms

    def count(self, name: str, n: int = 1) -> None:
        """记一个计数（例如「软线压缩是否真的触发了」）。"""
        self._counts[name] = self._counts.get(name, 0) + n

    def mark(self, name: str) -> None:
        """记录一个相对请求开始的时刻（例如 `first_frame`）。"""
        self._marks[name] = (time.monotonic() - self._t0) * 1000.0

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    # ---------- 输出 ----------

    def as_fields(self) -> dict[str, Any]:
        """展开成日志字段。段名直接做键（`add_msg=12`），最方便 grep。

        计数统一输出成 `<name>_n`，**独立于是否有同名 span**。
        早期的实现把计数挂在 span 名字下（`soft` -> `soft_called`），结果是
        「计数名和段名不一致时计数静默丢失」——这种"少一个字段"的错误在日志里
        完全看不出来，所以这里改成计数自成一路。
        """
        fields: dict[str, Any] = {}
        for name, ms in self._spans.items():
            fields[name] = round(ms, 1)
        for name, ms in self._marks.items():
            fields[name] = round(ms, 1)
        for name, n in self._counts.items():
            fields[f"{name}_n"] = n
        fields["total_ms"] = round(self.elapsed_ms(), 1)
        if self._exc:
            fields["exc"] = "; ".join(f"{k}={v}" for k, v in self._exc.items())
        return fields

    def emit(self, *, level: int = logging.INFO) -> None:
        """输出一行汇总。任何异常都吞掉——埋点不得影响请求。"""
        if not prelude_log_enabled():
            return
        try:
            logger.log(
                level,
                "首帧前奏 user=%s session=%s gen=%s total=%.0fms",
                self.user_id or "-",
                self.session_id or "-",
                (self.generation_id or "-")[:12],
                self.elapsed_ms(),
                extra={
                    "event": "chat.prelude",
                    "user_id": self.user_id,
                    "session_id": self.session_id,
                    "generation_id": self.generation_id,
                    **self.as_fields(),
                },
            )
        except Exception:  # noqa: BLE001 - 埋点绝不能拖垮请求
            pass


class _Span:
    """`PreludeTimer.span()` 的上下文管理器。"""

    __slots__ = ("_timer", "_name", "_t0")

    def __init__(self, timer: PreludeTimer, name: str) -> None:
        self._timer = timer
        self._name = name
        self._t0 = 0.0

    def __enter__(self) -> "_Span":
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        cost = (time.monotonic() - self._t0) * 1000.0
        self._timer.add(self._name, cost)
        if exc is not None:
            # 记下异常类型即可。真正的异常仍由调用方处理（返回 False 不吞异常），
            # 这里只是让汇总日志能显示「这一段炸了」，而不是显示成一个偏小的耗时。
            self._timer._exc[self._name] = type(exc).__name__
        return False


class async_span:
    """异步版：`async with async_span(timer, "runtime"): ...`。

    单独一个类而不是给 `_Span` 加 `__aenter__`，是因为同一个对象被当同步和
    异步使用时容易在异常路径上错配 finally。
    """

    __slots__ = ("_timer", "_name", "_t0")

    def __init__(self, timer: PreludeTimer, name: str) -> None:
        self._timer = timer
        self._name = name
        self._t0 = 0.0

    async def __aenter__(self) -> "async_span":
        self._t0 = time.monotonic()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        cost = (time.monotonic() - self._t0) * 1000.0
        self._timer.add(self._name, cost)
        if exc is not None:
            self._timer._exc[self._name] = type(exc).__name__
        return False
