"""看门狗**端到端**：卡住的生成器必须真的被中止，消息收敛成可诊断状态。

## 与 `test_turn_job_watchdog.py` 的分工

那个文件测 `turn_job` 单元（cancel 有没有发生）；
这个文件测**整条链路**：

    turn_job.start_job
      → _supervised
        → _wrapped → runner(= _run_job)
          → _event_stream (异步生成器)

关键问题：看门狗 cancel 的是外层任务，**内层生成器的 CancelledError
会不会真的传到 `_event_stream` 的 `except asyncio.CancelledError`**？
如果传不到，`finally` 里的 `prelude.emit()` 和 `_persist` 都不会执行 ——
那看门狗等于只是把外层 task 关掉，库里照样停在 `streaming`。

这正是真机事故的形态：`finally` 从未执行。所以这条必须测到生成器内部。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import turn_job


@pytest.fixture(autouse=True)
def _clean_jobs():
    turn_job._JOBS.clear()
    yield
    for job in list(turn_job._JOBS.values()):
        if job.task is not None and not job.task.done():
            job.task.cancel()
    turn_job._JOBS.clear()


def test_cancel_propagates_into_async_generator():
    """★ 核心：外层 cancel 必须让内层异步生成器收到 CancelledError。

    这是「finally 会不会执行」的前提。真机事故里 finally 没执行，
    所以库里停在 streaming；如果 cancel 传不下去，看门狗就治不了那个病。
    """
    seen: list[str] = []

    async def gen():
        try:
            seen.append("started")
            await asyncio.Event().wait()      # 永不返回
            yield "never"
        except asyncio.CancelledError:
            seen.append("cancelled")
            raise
        finally:
            seen.append("finally")

    async def runner(job):
        async for _ in gen():
            job.publish("x")

    async def main():
        job = turn_job.TurnJob("u_p", "s_p", "g_p", message_id="m_p")
        turn_job.start_job(job, runner, idle_timeout_s=0.1)
        await asyncio.sleep(0.5)
        return job

    job = asyncio.run(main())

    assert "started" in seen, "生成器根本没开始执行"
    assert "cancelled" in seen, (
        "看门狗 cancel 没传进异步生成器 —— finally 不会执行，库里会永远停在 streaming")
    assert "finally" in seen, (
        "生成器的 finally 没执行 —— prelude 日志和终态落库都会缺")
    assert job.watchdog_fired is True


def test_watchdog_reason_survives_to_job():
    """看门狗原因必须能读到（供上层写进消息/日志）。"""

    async def runner(_job):
        await asyncio.Event().wait()

    async def main():
        job = turn_job.TurnJob("u_r", "s_r", "g_r")
        turn_job.start_job(job, runner, idle_timeout_s=0.1)
        await asyncio.sleep(0.4)
        return job

    job = asyncio.run(main())
    assert job.watchdog_fired
    assert "闲置" in job.watchdog_reason or "idle" in job.watchdog_reason.lower()


def test_publish_between_waits_keeps_job_alive_longer_than_timeout():
    """跨多个阈值周期、但每周期都有 publish 的任务不能被杀。

    这条比 `test_turn_job_watchdog.py` 的同名测例更严：那里是 1 个阈值周期，
    这里跑 5 个周期，确认心跳不是「只重置一次」。
    """

    async def runner(job):
        for i in range(10):
            job.publish(f"event: tick\ndata: {i}\n\n")
            await asyncio.sleep(0.05)      # 10 × 0.05 = 0.5s，阈值 0.15s

    async def main():
        job = turn_job.TurnJob("u_k", "s_k", "g_k")
        turn_job.start_job(job, runner, idle_timeout_s=0.15)
        await asyncio.sleep(0.9)
        return job

    job = asyncio.run(main())
    assert job.watchdog_fired is False, (
        f"有持续进展仍被误杀（idle={job.idle_seconds():.2f}s）—— 心跳没起作用")
    assert job.seq == 10, f"帧数不对：{job.seq}"