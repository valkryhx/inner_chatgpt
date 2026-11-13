"""P1 首帧前奏计时：单测。

验收目标（docs/20261112-Hlaudz首帧串行阻塞核实与优化设计.md §5 的 P1 阶段）：
  1. 计时器能正确累计各段、记录时刻、记录计数；
  2. 异常路径下仍然记录耗时（不能漏段，否则数字永远偏小）；
  3. 埋点本身绝不抛异常（埋点不能成为新的故障点）；
  4. 默认开启、可显式关闭；
  5. `chat.prelude` 汇总日志的字段可以被 grep（`event` / 各段名 / `first_frame`）。

这些是 P2–P4 的验收基线：任何后续改造都必须让这些字段仍然存在且语义不变。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import prelude_timing  # noqa: E402
from app.prelude_timing import PreludeTimer, async_span, prelude_log_enabled  # noqa: E402


# --------------------------------------------------------------------------
# 基础计时
# --------------------------------------------------------------------------

def test_span_accumulates_same_name():
    """同名的多个 span 累加，而不是互相覆盖。

    真实场景：一轮里 `runtime` 只算一次，但若将来拆成多次调用，
    覆盖语义会让总耗时看起来比实际小。
    """
    t = PreludeTimer("u1", "co1", "g1")
    with t.span("x"):
        pass
    with t.span("x"):
        pass
    assert t.as_fields()["x"] >= 0.0
    # 两次累加后总量应 >= 单次（用极小的实际耗时，只验证是累加不是覆盖）
    t2 = PreludeTimer()
    t2.add("y", 5.0)
    t2.add("y", 7.0)
    assert t2.as_fields()["y"] == 12.0


def test_mark_records_absolute_elapsed():
    """mark 记录的是相对请求开始的时刻，不是偏移量。"""
    t = PreludeTimer()
    t.add("a", 1.0)
    t.mark("first_frame")
    fields = t.as_fields()
    assert "first_frame" in fields
    # first_frame 是绝对时刻，必然 >= 已经过去的时间
    assert fields["first_frame"] >= 0.0


def test_count_appears_as_suffix():
    """计数输出成 `<name>_n`，且**不依赖同名 span 是否存在**。

    真实用途：`soft` 段耗时为 0（没触发压缩）vs 根本没跑，靠
    `soft_called_n` 区分。早期实现把计数挂在同名 span 下，名字不一致时会
    静默丢失——这种"少一个字段"在日志里看不出来，所以单测锁死它。
    """
    t = PreludeTimer()
    t.add("soft", 0.0)
    t.count("soft_called")
    fields = t.as_fields()
    assert fields["soft"] == 0.0
    assert fields["soft_called_n"] == 1

    # 即使完全没有同名 span，计数也必须出现
    t2 = PreludeTimer()
    t2.count("standalone")
    assert t2.as_fields()["standalone_n"] == 1


def test_total_ms_present():
    t = PreludeTimer()
    with t.span("a"):
        pass
    assert "total_ms" in t.as_fields()


# --------------------------------------------------------------------------
# 异常路径
# --------------------------------------------------------------------------

def test_span_records_exception_and_reraises():
    """段内抛异常：耗时仍然记录，异常仍然向上抛（不能被埋点吞掉）。

    这是最容易写错的一条：如果用 try/finally 但忘了记异常名，
    汇总日志里那一段会显示成一个正常的小耗时，反而掩盖了问题。
    """
    t = PreludeTimer()

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with t.span("bad"):
            raise Boom("x")

    fields = t.as_fields()
    assert "bad" in fields
    assert "Boom" in fields["exc"]


def test_emit_never_raises(monkeypatch):
    """emit 内部任何异常都必须被吞掉——埋点不能拖垮请求。"""
    t = PreludeTimer()

    def boom(*_a, **_k):
        raise RuntimeError("log exploded")

    monkeypatch.setattr(prelude_timing.logger, "log", boom)
    t.emit()  # 不抛


def test_span_does_not_swallow_exception():
    """同步 span 返回 False（不吞异常）。"""
    t = PreludeTimer()
    with pytest.raises(ValueError):
        with t.span("x"):
            raise ValueError("y")


# --------------------------------------------------------------------------
# 异步 span
# --------------------------------------------------------------------------

def test_async_span_measures_and_records():
    async def main():
        t = PreludeTimer()
        async with async_span(t, "runtime"):
            await asyncio.sleep(0.01)
        return t

    t = asyncio.run(main())
    assert t.as_fields()["runtime"] >= 5.0  # 至少睡到 5ms 级别


def test_async_span_records_exception():
    async def main():
        t = PreludeTimer()
        with pytest.raises(RuntimeError):
            async with async_span(t, "soft"):
                raise RuntimeError("boom")
        return t

    t = asyncio.run(main())
    fields = t.as_fields()
    assert "soft" in fields
    assert "RuntimeError" in fields["exc"]


# --------------------------------------------------------------------------
# 开关
# --------------------------------------------------------------------------

def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv("HLZ_PRELUDE_LOG", raising=False)
    assert prelude_log_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_can_be_disabled(monkeypatch, value):
    monkeypatch.setenv("HLZ_PRELUDE_LOG", value)
    assert prelude_log_enabled() is False


def test_emit_silent_when_disabled(monkeypatch):
    monkeypatch.setenv("HLZ_PRELUDE_LOG", "0")
    calls = []
    monkeypatch.setattr(prelude_timing.logger, "log",
                        lambda *a, **k: calls.append((a, k)))
    PreludeTimer().emit()
    assert calls == []


# --------------------------------------------------------------------------
# 日志字段可 grep（P1 的实际验收点）
# --------------------------------------------------------------------------

def test_emit_fields_are_greppable(monkeypatch, caplog):
    """汇总日志必须同时带：event 名、各段名、first_frame、total_ms。

    这是 P1 的验收核心 —— 后续 P2–P4 改完，运维要能直接 grep
    `event=chat.prelude` 并且看到 `soft` 段仍然存在。
    """
    monkeypatch.delenv("HLZ_PRELUDE_LOG", raising=False)
    captured: list[dict] = []
    monkeypatch.setattr(
        prelude_timing.logger, "log",
        lambda *_a, **kw: captured.append(kw.get("extra") or {}),
    )

    t = PreludeTimer("u_x", "co_x", "g_x")
    t.add("add_msg", 12.0)
    t.add("runtime", 8.0)
    t.add("soft", 5000.0)
    t.count("soft_called")
    t.mark("first_frame")
    t.emit()

    assert len(captured) == 1
    extra = captured[0]
    assert extra["event"] == "chat.prelude"
    assert extra["user_id"] == "u_x"
    assert extra["session_id"] == "co_x"
    assert extra["add_msg"] == 12.0
    assert extra["soft"] == 5000.0
    assert extra["soft_called_n"] == 1
    assert "first_frame" in extra
    assert "total_ms" in extra


def test_generation_id_truncated_in_message_but_full_in_extra(monkeypatch):
    """消息体里 gen 截断（好读），extra 里保留完整 id（好 grep）。"""
    monkeypatch.delenv("HLZ_PRELUDE_LOG", raising=False)
    records: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        prelude_timing.logger, "log",
        lambda *a, **kw: records.append((a, kw)),
    )
    gid = "g_" + "a" * 32
    PreludeTimer("u", "co", gid).emit()
    args, kwargs = records[0]
    # 消息体（args[1] 是格式化串，args[3] 是 gen 参数）
    assert any(isinstance(a, str) and a.startswith("g_") and len(a) == 12 for a in args)
    assert kwargs["extra"]["generation_id"] == gid


# --------------------------------------------------------------------------
# 真机缺陷回归：字段必须真的能落盘（2026-11-12 实测踩到）
# --------------------------------------------------------------------------

def test_prelude_uses_dedicated_logger_channel():
    """必须走 `hlaudz.prelude` 通道，而不是模块级 root logger。

    为什么锁这条：普通 Formatter 不渲染 `extra`，所以走 root 时
    `soft=5000` 这类字段会**全部丢失**，只剩一句人话。
    真机实测（2026-11-12 09:22:26）app.log 里只有 total，各段耗时一个都没有。
    这条测试防止有人"顺手"把 logger 改回 `getLogger(__name__)`。
    """
    from app.logging_setup import PRELUDE_LOGGER_NAME

    assert prelude_timing.logger.name == PRELUDE_LOGGER_NAME


def test_jsonl_formatter_renders_span_fields():
    """用真实的 JsonLineFormatter 渲染，确认各段耗时确实出现在 JSON 里。"""
    import json

    from app.logging_setup import JsonLineFormatter

    t = PreludeTimer("u_j", "co_j", "g_j")
    t.add("soft", 5000.0)
    t.count("soft_called")
    t.mark("first_frame")

    record = logging.LogRecord(
        name="hlaudz.prelude", level=logging.INFO, pathname=__file__, lineno=1,
        msg="首帧前奏 total=5001ms", args=(), exc_info=None,
    )
    record.__dict__.update({"event": "chat.prelude", **t.as_fields()})

    payload = json.loads(JsonLineFormatter().format(record))
    assert payload["event"] == "chat.prelude"
    assert payload["soft"] == 5000.0
    assert payload["soft_called_n"] == 1
    assert "first_frame" in payload
    assert "total_ms" in payload
