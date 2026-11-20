"""prelude 日志的字段名必须不自相矛盾。

## 为什么

2026-11-20 我两次被这份日志误导，写下了「前奏跑了 24.4 分钟」的结论，
并据此设计了一整套「前奏性能优化」方案 —— 而前奏其实只有 **10 毫秒**。

根因是字段名与文案不一致：`emit()` 在 `_event_stream` 的 `finally` 里执行
（整轮结束），所以

- `total_ms` 实际是**整轮**耗时（含子代理调研与写作）
- 而日志文案写的是「首帧前奏 … total=1465098ms」

任何人读到都会以为前奏花了一千多秒。字段名必须能自证，不能靠读源码才知道。

## 判据

`emit()` 产出的字段里：
- 必须有 `turn_ms`（整轮），语义与文案一致；
- 必须有 `first_frame_ms`（真正的首帧时刻）；
- 不能再有裸的 `total_ms`（它是歧义源）。
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import prelude_timing as pt  # noqa: E402


def _fields_from_emit(monkeypatch) -> dict:
    t = pt.PreludeTimer("u1", "co_x", "g_x")
    t.mark("first_frame")
    captured: dict = {}

    class _H(logging.Handler):
        def emit(self, record):
            captured.update(record.__dict__.get("extra_fields") or {})

    logger = logging.getLogger(pt.PRELUDE_LOGGER_NAME)
    h = _H()
    monkeypatch.setattr(logger, "log",
                        lambda lvl, msg, *a, **kw: captured.update(kw.get("extra") or {}))
    t.emit()
    return captured


def test_has_turn_ms_and_first_frame_ms(monkeypatch):
    f = _fields_from_emit(monkeypatch)
    assert "turn_ms" in f, f"缺整轮耗时字段（现有：{sorted(f)}）"
    assert "first_frame_ms" in f, f"缺首帧时刻字段（现有：{sorted(f)}）"


def test_no_ambiguous_total_ms(monkeypatch):
    f = _fields_from_emit(monkeypatch)
    assert "total_ms" not in f, (
        "`total_ms` 语义歧义：它在 finally 里量的是整轮，"
        "文案却写成「首帧前奏 total」——正是它导致 24.4 分钟的误判")


def test_first_frame_is_milliseconds_not_minutes(monkeypatch):
    """首帧必须是毫秒级。这条锁住「前奏不慢」这个实测事实。"""
    f = _fields_from_emit(monkeypatch)
    assert f["first_frame_ms"] < 1000, (
        f"first_frame_ms={f['first_frame_ms']} —— 前奏不该以秒计")
