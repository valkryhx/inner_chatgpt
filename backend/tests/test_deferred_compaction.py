"""P4 软线压缩延后：单测。

验收目标（docs/20261112-Hlaudz首帧串行阻塞核实与优化设计.md §3.2 改造二 A）：
  1. 赛后压缩不阻塞本轮（`done` 之后才调度，不 await）；
  2. 同一会话不重复启动（幂等）；
  3. **新一轮能抢占**上一轮的赛后压缩（否则上一轮的摘要会拖住下一轮首帧）；
  4. 赛后压缩失败静默（不能让用户看到错误）；
  5. 超时放弃（下一轮会再试）；
  6. 开关 `HLZ_SOFT_COMPACT_DEFER=0` 退回赛前压缩。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import deferred_compact  # noqa: E402
from app.deferred_compact import (  # noqa: E402
    pending_count,
    preempt,
    schedule_soft_compact,
    soft_defer_enabled,
)


@pytest.fixture(autouse=True)
def _clean_pending():
    """每个用例前后清空模块级状态，避免用例互相污染。"""
    deferred_compact._pending.clear()
    yield
    for t in list(deferred_compact._pending.values()):
        if not t.done():
            t.cancel()
    deferred_compact._pending.clear()


def _install_fake_soft(monkeypatch, fn):
    """把 `compaction.enforce_soft_limit` 换成测试替身。

    `deferred_compact._run` 是延迟导入 `from .compaction import enforce_soft_limit`，
    所以必须替换 `compaction` 模块上的属性。
    """
    import app.compaction as compaction
    monkeypatch.setattr(compaction, "enforce_soft_limit", fn)


# --------------------------------------------------------------------------
# 开关
# --------------------------------------------------------------------------

def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv("HLZ_SOFT_COMPACT_DEFER", raising=False)
    assert soft_defer_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_can_be_disabled(monkeypatch, value):
    monkeypatch.setenv("HLZ_SOFT_COMPACT_DEFER", value)
    assert soft_defer_enabled() is False


def test_disabled_schedules_nothing(monkeypatch):
    monkeypatch.setenv("HLZ_SOFT_COMPACT_DEFER", "0")
    assert schedule_soft_compact("u", "s") is None
    assert pending_count() == 0


# --------------------------------------------------------------------------
# 调度幂等
# --------------------------------------------------------------------------

def test_schedule_is_idempotent():
    """同一会话重复调度时返回同一个 task，不重复启动。"""
    async def main():
        gate = asyncio.Event()

        async def slow(*_a, **_k):
            await gate.wait()

        # 直接替换 _run 的行为通过 gate 控制
        import app.compaction as compaction
        orig = compaction.enforce_soft_limit
        compaction.enforce_soft_limit = slow
        try:
            t1 = schedule_soft_compact("u", "s")
            t2 = schedule_soft_compact("u", "s")
            same = t1 is t2
            n = pending_count()
            gate.set()
            await asyncio.sleep(0.01)
            return same, n
        finally:
            compaction.enforce_soft_limit = orig

    same, n = asyncio.run(main())
    assert same is True
    assert n == 1


def test_different_sessions_are_independent():
    async def main():
        gate = asyncio.Event()

        async def slow(*_a, **_k):
            await gate.wait()

        import app.compaction as compaction
        orig = compaction.enforce_soft_limit
        compaction.enforce_soft_limit = slow
        try:
            schedule_soft_compact("u", "s1")
            schedule_soft_compact("u", "s2")
            n = pending_count()
            gate.set()
            await asyncio.sleep(0.01)
            return n
        finally:
            compaction.enforce_soft_limit = orig

    assert asyncio.run(main()) == 2


# --------------------------------------------------------------------------
# 抢占（P4 的关键安全机制）
# --------------------------------------------------------------------------

def test_preempt_cancels_inflight_compaction():
    """新一轮必须能抢占上一轮的赛后压缩，否则上一轮的摘要会拖住本轮首帧。"""
    async def main():
        started = asyncio.Event()

        async def stuck(*_a, **_k):
            started.set()
            await asyncio.Event().wait()      # 永不完成

        import app.compaction as compaction
        orig = compaction.enforce_soft_limit
        compaction.enforce_soft_limit = stuck
        try:
            t = schedule_soft_compact("u", "s")
            await asyncio.wait_for(started.wait(), timeout=2.0)
            got = await preempt("u", "s")
            return got, t.cancelled() or t.done(), pending_count()
        finally:
            compaction.enforce_soft_limit = orig

    got, finished, pending = asyncio.run(main())
    assert got is True, "没有抢占到在飞任务"
    assert finished is True, "任务没有被取消/结束"
    assert pending == 0


def test_preempt_returns_false_when_nothing_inflight():
    assert asyncio.run(preempt("u", "nothing")) is False


def test_preempt_returns_false_when_already_done():
    async def main():
        async def quick(*_a, **_k):
            return None

        import app.compaction as compaction
        orig = compaction.enforce_soft_limit
        compaction.enforce_soft_limit = quick
        try:
            t = schedule_soft_compact("u", "s")
            await asyncio.wait_for(asyncio.shield(t), timeout=2.0)
            await asyncio.sleep(0.01)
            return await preempt("u", "s")
        finally:
            compaction.enforce_soft_limit = orig

    assert asyncio.run(main()) is False


# --------------------------------------------------------------------------
# 失败与超时
# --------------------------------------------------------------------------

def test_failure_is_silent(caplog):
    """赛后压缩失败必须静默：只记 warning，不抛给任何人。"""
    async def main():
        async def boom(*_a, **_k):
            raise RuntimeError("compact exploded")

        import app.compaction as compaction
        orig = compaction.enforce_soft_limit
        compaction.enforce_soft_limit = boom
        try:
            t = schedule_soft_compact("u", "s")
            await asyncio.wait_for(asyncio.shield(t), timeout=2.0)
            return t.exception() if not t.cancelled() else "cancelled"
        finally:
            compaction.enforce_soft_limit = orig

    # `_run` 内部吞掉异常，所以 task 本身不该带异常
    assert asyncio.run(main()) is None
    assert any("赛后压缩失败" in str(r.msg) for r in caplog.records)


def test_timeout_gives_up(monkeypatch, caplog):
    """超时后放弃（下一轮会再试），不能让后台任务无限跑。"""
    monkeypatch.setenv("HLZ_SOFT_COMPACT_TIMEOUT_S", "0.05")

    async def main():
        async def stuck(*_a, **_k):
            await asyncio.Event().wait()

        import app.compaction as compaction
        orig = compaction.enforce_soft_limit
        compaction.enforce_soft_limit = stuck
        try:
            t = schedule_soft_compact("u", "s")
            await asyncio.wait_for(asyncio.shield(t), timeout=3.0)
            return True
        finally:
            compaction.enforce_soft_limit = orig

    assert asyncio.run(main()) is True
    assert any("放弃" in str(r.msg) for r in caplog.records)


def test_timeout_env_parsing(monkeypatch):
    monkeypatch.setenv("HLZ_SOFT_COMPACT_TIMEOUT_S", "12")
    assert deferred_compact.deferred_timeout_from_env() == 12.0
    monkeypatch.setenv("HLZ_SOFT_COMPACT_TIMEOUT_S", "abc")
    assert deferred_compact.deferred_timeout_from_env() == deferred_compact.DEFAULT_DEFERRED_TIMEOUT_S
    monkeypatch.setenv("HLZ_SOFT_COMPACT_TIMEOUT_S", "-1")
    assert deferred_compact.deferred_timeout_from_env() == 0.0


# --------------------------------------------------------------------------
# 源码级结构断言（防"顺手改回赛前压缩"）
# --------------------------------------------------------------------------

def test_chat_schedules_after_done_not_before():
    """赛后压缩的调度必须在 `done` 发出**之后**。

    这条一旦被改回去（回到赛前压缩），用户又要在看到「完成」之前等摘要几秒。
    功能上看不出差别，只有超软线会话的用户能感知。
    """
    import inspect

    from app.routers import chat

    src = inspect.getsource(chat._event_stream)
    pos_done = src.find('yield frame("done"')
    pos_sched = src.find("schedule_soft_compact")
    assert pos_done >= 0 and pos_sched >= 0
    assert pos_done < pos_sched, "赛后压缩登记在 done 之前 —— 会阻塞用户看到完成"


def test_soft_compact_not_awaited_before_model_call():
    """赛前路径不得再无条件 await 软线压缩（默认配置下应走延后分支）。"""
    import inspect

    from app.routers import chat

    src = inspect.getsource(chat._event_stream)
    # 赛前软线压缩必须被 soft_defer_enabled() 开关包住
    assert "soft_defer_enabled" in src
    i_guard = src.find("soft_defer_enabled")
    i_await = src.find("await enforce_soft_limit")
    # 若存在赛前 await，它必须在开关的 else 分支之后
    if i_await >= 0:
        assert i_guard < i_await


def test_hard_limit_preempts_deferred_compaction():
    """硬线裁剪前必须抢占赛后压缩（否则等锁会拖住首帧）。"""
    import inspect

    from app.routers import chat

    src = inspect.getsource(chat._event_stream)
    pos_preempt = src.find("_preempt_compact")
    pos_hard = src.find("enforce_hard_limit")
    assert pos_preempt >= 0, "找不到抢占调用"
    assert pos_hard >= 0
    assert pos_preempt < pos_hard, "抢占必须在硬线裁剪之前"