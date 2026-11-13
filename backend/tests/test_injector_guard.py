"""P2 注入器超时预算：单测。

验收目标（docs/20261112-Hlaudz首帧串行阻塞核实与优化设计.md §3.3 / §5 的 P2）：
  1. 注入器卡死时，本轮仍在超时后继续（不再永不结束）；
  2. **不丢消息**：超时的任务在后台跑完，下一轮取到结果；
  3. 超时不 cancel 注入器（否则 consume_inbox 已 commit 的消息会丢）；
  4. 上一轮未完成时不叠加新调用（避免两次 consume_inbox 并发）；
  5. 同步注入器零开销（不建 task）；
  6. 注入器异常不影响本轮；
  7. 默认超时 8s > DB busy_timeout 5s（防止把正常锁等待误判为卡死）。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.injector_guard import (  # noqa: E402
    DEFAULT_INJECTOR_TIMEOUT_S,
    InjectorGuard,
    injector_timeout_from_env,
)


# --------------------------------------------------------------------------
# 基本行为
# --------------------------------------------------------------------------

def test_sync_injector_is_passed_through():
    """同步注入器直接返回，结果原样保留。"""
    guard = InjectorGuard(lambda: [{"role": "user", "content": "hi"}], 1.0)
    msgs, timed_out = asyncio.run(guard.collect())
    assert msgs == [{"role": "user", "content": "hi"}]
    assert timed_out is False


def test_dirty_return_values_are_filtered():
    """脏返回值不能让整轮失败：只保留 dict。"""
    guard = InjectorGuard(lambda: [{"a": 1}, "junk", None, 42], 1.0)
    msgs, _ = asyncio.run(guard.collect())
    assert msgs == [{"a": 1}]

    guard2 = InjectorGuard(lambda: None, 1.0)
    assert asyncio.run(guard2.collect())[0] == []


def test_async_injector_returns_result():
    async def inj():
        await asyncio.sleep(0.01)
        return [{"role": "user", "content": "async"}]

    guard = InjectorGuard(inj, 5.0)
    msgs, timed_out = asyncio.run(guard.collect())
    assert msgs == [{"role": "user", "content": "async"}]
    assert timed_out is False


# --------------------------------------------------------------------------
# 核心：超时 + 不丢消息
# --------------------------------------------------------------------------

def test_slow_injector_times_out_but_does_not_block_forever():
    """注入器卡住时，本轮在超时后继续（这就是 P2 要修的核心缺陷）。"""
    async def main():
        released = asyncio.Event()

        async def slow():
            await released.wait()          # 永不主动结束
            return [{"role": "user", "content": "late"}]

        guard = InjectorGuard(slow, 0.05)
        msgs, timed_out = await guard.collect()
        released.set()
        await guard.flush()                # 收尾，避免 task 泄漏
        return msgs, timed_out

    msgs, timed_out = asyncio.run(main())
    assert timed_out is True
    assert msgs == []                      # 本轮没拿到


def test_timed_out_task_is_not_cancelled_and_result_arrives_next_round():
    """**不丢消息**：超时的任务继续跑，下一轮 collect() 取到它的结果。

    这是本模块存在的根本理由。若改用裸 `asyncio.wait_for`，
    超时会 cancel 注入器，而 consume_inbox 已经把消息标成 consumed，
    那批消息就永久丢失了。
    """
    async def main():
        produced = asyncio.Event()

        async def slow():
            await asyncio.sleep(0.08)      # 比超时长
            produced.set()
            return [{"role": "user", "content": "delivered-late"}]

        guard = InjectorGuard(slow, 0.02)
        first, timed_out = await guard.collect()
        assert timed_out is True
        assert first == []

        # 等后台任务真的跑完
        await asyncio.wait_for(produced.wait(), timeout=2.0)
        await asyncio.sleep(0.01)

        # 下一轮：拿到上一轮迟到的结果，且**不会再次调用注入器**
        second, timed_out2 = await guard.collect()
        assert second == [{"role": "user", "content": "delivered-late"}]
        assert timed_out2 is False
        return True

    assert asyncio.run(main()) is True


def test_no_overlapping_invocation_while_previous_pending():
    """上一轮任务未完成时，本轮不叠加新调用。

    叠加会并发跑两次 consume_inbox，把同一批团队消息拆到两条注入路径上，
    既可能重复注入也可能乱序。
    """
    async def main():
        calls = {"n": 0}
        release = asyncio.Event()

        async def slow():
            calls["n"] += 1
            await release.wait()
            return []

        guard = InjectorGuard(slow, 0.02)
        await guard.collect()              # 第一次，超时
        await guard.collect()              # 第二次：上一轮还没完
        await guard.collect()              # 第三次：仍然不叠加
        n = calls["n"]
        release.set()
        await guard.flush()
        return n

    assert asyncio.run(main()) == 1


def test_flush_logs_error_when_task_never_finishes(caplog):
    """收尾时任务仍未完成：必须留下**明确**的丢失痕迹，不能沉默。"""
    async def main():
        never = asyncio.Event()

        async def stuck():
            await never.wait()
            return []

        guard = InjectorGuard(stuck, 0.02)
        await guard.collect()
        got = await guard.flush()
        return got

    got = asyncio.run(main())
    assert got == []
    assert any("仍未返回" in r.message or "仍未返回" in str(r.msg)
               for r in caplog.records)


def test_flush_delivers_result_when_task_finishes_in_grace():
    """收尾宽限内完成：结果能被交出来（不丢）。"""
    async def main():
        async def slow():
            await asyncio.sleep(0.05)
            return [{"role": "user", "content": "in-grace"}]

        guard = InjectorGuard(slow, 0.01)
        await guard.collect()              # 超时
        return await guard.flush()         # 宽限 2s 内完成

    got = asyncio.run(main())
    assert got == [{"role": "user", "content": "in-grace"}]


# --------------------------------------------------------------------------
# 异常隔离
# --------------------------------------------------------------------------

def test_sync_injector_exception_is_swallowed():
    def boom():
        raise RuntimeError("injector exploded")

    guard = InjectorGuard(boom, 1.0)
    msgs, timed_out = asyncio.run(guard.collect())
    assert msgs == []
    assert timed_out is False          # 是失败，不是超时


def test_async_injector_exception_is_swallowed():
    async def boom():
        raise RuntimeError("async exploded")

    guard = InjectorGuard(boom, 5.0)
    msgs, timed_out = asyncio.run(guard.collect())
    assert msgs == []
    assert timed_out is False


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def test_default_timeout_exceeds_db_busy_timeout():
    """默认超时必须 > DB 的 busy_timeout（5000ms）。

    否则「等 DB 锁」这种正常行为会被误判成「注入器卡死」，
    在并发高的机器上会造成本可以正常注入的消息被推迟。
    """
    assert DEFAULT_INJECTOR_TIMEOUT_S >= 5.0


def test_env_override(monkeypatch):
    monkeypatch.setenv("HLZ_INJECTOR_TIMEOUT_S", "12")
    assert injector_timeout_from_env() == 12.0


def test_env_zero_disables_timeout(monkeypatch):
    """0 / 负数 = 关闭超时（退回历史行为，无限等）。"""
    monkeypatch.setenv("HLZ_INJECTOR_TIMEOUT_S", "0")
    assert injector_timeout_from_env() == 0.0
    monkeypatch.setenv("HLZ_INJECTOR_TIMEOUT_S", "-3")
    assert injector_timeout_from_env() == 0.0


def test_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("HLZ_INJECTOR_TIMEOUT_S", "abc")
    assert injector_timeout_from_env() == DEFAULT_INJECTOR_TIMEOUT_S


def test_timeout_zero_waits_indefinitely():
    """显式关闭超时时，慢注入器仍会被等到（保持历史语义）。"""
    async def main():
        async def slow():
            await asyncio.sleep(0.05)
            return [{"role": "user", "content": "waited"}]

        guard = InjectorGuard(slow, 0.0)
        return await guard.collect()

    msgs, timed_out = asyncio.run(main())
    assert msgs == [{"role": "user", "content": "waited"}]
    assert timed_out is False


def test_timeout_hits_counter():
    async def main():
        release = asyncio.Event()

        async def slow():
            await release.wait()
            return []

        guard = InjectorGuard(slow, 0.01)
        await guard.collect()
        hits = guard.timeout_hits
        release.set()
        await guard.flush()
        return hits

    assert asyncio.run(main()) == 1