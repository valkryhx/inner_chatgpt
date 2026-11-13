"""P2 集成：`AgentLoop` 真的接上了注入器超时预算。

与 `test_injector_guard.py` 的分工：那边测 guard 自身的语义，这里测
「agent_loop 是否真的用它 + 超时后本轮是否继续 + 事件形状是否正确」。

背景（2026-11-12 实测）：注入点是裸 await 且全文件无 timeout，
注入器卡死等于整轮永不结束。见
docs/20261112-Hlaudz首帧串行阻塞核实与优化设计.md §1.3。
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent_loop import AgentLoop  # noqa: E402
from app.tools import ToolRegistry  # noqa: E402


class FakeClient:
    """最小 LLM client 替身：跑完一轮就返回不含 tool_calls 的回复。"""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, messages, tools, on_delta=None, stop_check=None):
        self.calls += 1
        if on_delta:
            on_delta("text", "ok")

        class _R:
            content = "ok"
            reasoning = ""
            tool_calls: list[Any] = []
            finish_reason = "stop"

        return _R()


def _loop(client, *, injector=None, timeout: float = 0.05) -> AgentLoop:
    loop = AgentLoop(client, ToolRegistry(), "sys", max_turns=2)
    loop.message_injector = injector
    loop._injector_timeout_s = timeout
    return loop


async def _drain(loop, history=None):
    history = history if history is not None else [{"role": "user", "content": "hi"}]
    return [ev async for ev in loop.run_stream(history, session_id="s1")], history


# --------------------------------------------------------------------------
# 真实缺陷回归：注入器卡死不再让整轮永不结束
# --------------------------------------------------------------------------

def test_hanging_injector_does_not_hang_the_turn():
    """注入器永不返回时，本轮仍然跑完（这是 P2 的核心验收）。"""
    async def main():
        release = asyncio.Event()

        async def stuck():
            await release.wait()
            return []

        loop = _loop(FakeClient(), injector=stuck, timeout=0.05)
        try:
            events, _ = await asyncio.wait_for(_drain(loop), timeout=5.0)
        finally:
            release.set()
        return events

    events = asyncio.run(main())
    assert any(e["type"] == "done" for e in events)
    assert any(e["type"] == "injector_timeout" for e in events)


def test_injector_timeout_event_carries_the_budget():
    """超时事件要带出预算值，否则用户/运维不知道能调哪个开关。"""
    async def main():
        release = asyncio.Event()

        async def stuck():
            await release.wait()
            return []

        loop = _loop(FakeClient(), injector=stuck, timeout=0.05)
        try:
            events, _ = await asyncio.wait_for(_drain(loop), timeout=5.0)
        finally:
            release.set()
        return events

    events = asyncio.run(main())
    ev = next(e for e in events if e["type"] == "injector_timeout")
    assert ev["timeout_s"] == 0.05


# --------------------------------------------------------------------------
# 正常路径不变
# --------------------------------------------------------------------------

def test_normal_injection_still_works():
    """同步注入器：消息照常进 history 并发 message_injected（无回归）。"""
    def injector():
        return [{"role": "user", "content": "[任务通知] 子代理完成"}]

    async def main():
        loop = _loop(FakeClient(), injector=injector, timeout=5.0)
        return await _drain(loop)

    events, history = asyncio.run(main())
    injected = [e for e in events if e["type"] == "message_injected"]
    assert len(injected) == 1
    assert "[任务通知]" in injected[0]["text"]
    assert any(m.get("content", "").startswith("[任务通知]") for m in history)
    # 正常路径不该冒超时事件
    assert not any(e["type"] == "injector_timeout" for e in events)


def test_async_injector_works_on_normal_path():
    async def injector():
        await asyncio.sleep(0.01)
        return [{"role": "user", "content": "[来自 reader 的团队消息] 结论如下"}]

    async def main():
        loop = _loop(FakeClient(), injector=injector, timeout=5.0)
        return await _drain(loop)

    events, history = asyncio.run(main())
    assert any(e["type"] == "message_injected" for e in events)
    assert any("团队消息" in m.get("content", "") for m in history)


def test_injector_exception_does_not_break_the_turn():
    def boom():
        raise RuntimeError("injector exploded")

    async def main():
        loop = _loop(FakeClient(), injector=boom, timeout=5.0)
        return await _drain(loop)

    events, _ = asyncio.run(main())
    assert any(e["type"] == "done" for e in events)
    # 异常不是超时，不该混淆
    assert not any(e["type"] == "injector_timeout" for e in events)


# --------------------------------------------------------------------------
# 惰性绑定（消息注入器会被重新赋值，guard 必须跟着换）
# --------------------------------------------------------------------------

def test_guard_rebinds_when_injector_is_replaced():
    """`message_injector` 在构造后被赋值（host.py / sse.py 都这么做）。

    若 guard 在 init 时绑死，那两处赋值之后注入会静默失效。
    """
    async def main():
        loop = AgentLoop(FakeClient(), ToolRegistry(), "sys", max_turns=2)
        assert loop.message_injector is None

        # 构造之后才装注入器 —— 模拟 subagent/host.py:162 的做法
        loop.message_injector = lambda: [{"role": "user", "content": "late-bound"}]
        loop._injector_timeout_s = 5.0
        events, history = await _drain(loop)
        return events, history

    events, history = asyncio.run(main())
    assert any(e["type"] == "message_injected" for e in events)
    assert any(m.get("content") == "late-bound" for m in history)


def test_flush_is_called_on_normal_completion():
    """收尾 flush 不应抛异常，也不应影响正常结束。"""
    async def main():
        loop = _loop(FakeClient(), injector=lambda: [], timeout=5.0)
        await _drain(loop)
        # guard 存在且没有遗留任务
        assert loop._injector_guard is not None
        assert loop._injector_guard.pending is False

    asyncio.run(main())