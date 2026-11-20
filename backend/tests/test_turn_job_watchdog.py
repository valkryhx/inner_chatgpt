"""看门狗：整轮生成/前奏长时间无进展时必须被中止，而不是静默挂死。

## ★ 前提订正（2026-11-20 当晚回查）

这份文件最初写的动机是「`co_f49ca93ebc14` 卡死了」。**那个结论是错的** ——
回查终态：`researcher` 28 轮 completed、`writer` 20 轮 completed、
两个产物 51,755 / 70,389 B、消息 `complete`。它只是**慢**（前奏/整轮 1465s），
并没有挂死。详见 `docs/20261120-AgentTeam修复后e2e验证.md` §10。

所以保留这份测试的理由不是「治那次事故」，而是：

1. 当时确实没有**任何**超时机制，这是真实的能力缺口；
2. 将来真出现挂死时，需要能诊断（区分用户停止 vs 看门狗中止）；
3. CC319 有等价机制（`m0779` 的 stall watchdog），对齐它可以避免误杀。

## 判据（必须能区分两种状态）

- 无看门狗：`start_job` 的任务永不结束，`job.finished` 恒 False
- 有看门狗：超过阈值后被 `cancel()`，`job.finished` 转 True，任务退出
- **工具在飞时不中止**（`tool_in_flight > 0` → deferred，对齐 CC 的 `Fe`）
- 工具返回后继续闲置 → 仍会中止（deferred ≠ 永不触发）

阈值通过参数注入（默认 30 分钟对生产合适，测试用 0.1s）。
"""
from __future__ import annotations

import asyncio

import pytest

from app import turn_job


@pytest.fixture(autouse=True)
def _clean_jobs():
    """每个测例前后清空全局 job 表，避免互相污染。"""
    turn_job._JOBS.clear()
    yield
    for job in list(turn_job._JOBS.values()):
        if job.task is not None and not job.task.done():
            job.task.cancel()
    turn_job._JOBS.clear()


def _make_job(gen: str = "g_wd") -> turn_job.TurnJob:
    return turn_job.TurnJob("u_wd", "s_wd", gen, message_id="m_wd")


async def _hang(_job):
    """模拟「卡在一个永不返回的 await」。"""
    await asyncio.Event().wait()


async def _hang_forever_in_stages(job, stage_events: list[str]):
    """模拟卡在前奏某一段：先打一个标记，然后永久阻塞。

    `stage_events` 用来证明「卡住前确实进过某一段」——这是真实事故的形态
    （`add_msg` 执行了，然后卡在后面某个 await）。
    """
    stage_events.append("before_hang")
    await asyncio.Event().wait()


def test_watchdog_cancels_stuck_job():
    """★ 核心：卡住的 job 必须被看门狗中止。

    ## ⚠️ 第一版这条是假绿（反向确认抓出来的）

    第一版断言：

        job = asyncio.run(main())          # main 里 sleep 0.5 后 return
        assert job.finished is True

    看起来对，实际**永远通过**：`asyncio.run()` 在 `main()` 返回时会
    取消所有 pending 任务并等待它们结束 —— 那个取消本身就让
    `_supervised` 收尾、`_reap` 把 `finished` 置 True。

    于是「看门狗把任务杀掉」和「事件循环退出把任务杀掉」在断言上
    完全一样。实测注入「看门狗永远 return」后这条测试仍绿。

    ## 正确的判据

    1. **必须在事件循环还活着的时候取样**（在 `main()` 内部断言）；
    2. 断言 `watchdog_fired`（这是唯一能区分两种状态的信号），
       而不是 `finished`（两个原因都会导致它）；
    3. 断言主任务确实被 cancel 了（`main_task.cancelled()`）。
    """

    async def main():
        job = _make_job()
        turn_job.start_job(job, _hang, idle_timeout_s=0.15)
        await asyncio.sleep(0.5)
        # —— 在循环还活着时取样，避免 asyncio.run 退出时的取消掩盖真相 ——
        fired = job.watchdog_fired
        main_done = job.main_task is not None and job.main_task.done()
        was_cancelled = (job.main_task.cancelled()
                         if job.main_task is not None else False)
        return fired, main_done, was_cancelled

    fired, main_done, was_cancelled = asyncio.run(main())
    assert fired is True, (
        "看门狗没触发 —— 卡住的 job 会永久占用租约"
        "（注意：asyncio.run 退出也会取消任务，所以不能只看 finished）")
    assert main_done, "主任务仍在跑"
    assert was_cancelled, "主任务不是被 cancel 结束的（应看门狗 cancel，不是自然完成）"


def test_watchdog_does_not_kill_healthy_job():
    """★ 负向：正常完成的 job **不能**被看门狗误杀。

    只测「该杀时杀了」会漏掉「不该杀也杀了」——那样所有长任务都会被砍断。
    """

    async def quick(_job):
        await asyncio.sleep(0.05)

    async def main():
        job = _make_job("g_ok")
        turn_job.start_job(job, quick, idle_timeout_s=5.0)
        await asyncio.sleep(0.3)
        return job

    job = asyncio.run(main())
    assert job.finished is True
    # 正常结束：任务自然完成，不是被取消
    assert job.watchdog_fired is False, (
        "正常完成的 job 被看门狗判成卡住 —— 阈值或心跳逻辑错了")


def test_watchdog_marks_reason_on_job():
    """被看门狗中止的 job 必须留下可诊断的标记。

    没有这个标记，「为什么这条消息是 stopped」就无从查起 ——
    与用户主动停止看起来完全一样。
    """

    async def main():
        job = _make_job("g_mark")
        turn_job.start_job(job, _hang, idle_timeout_s=0.1)
        await asyncio.sleep(0.4)
        return job.watchdog_fired, job.watchdog_reason

    fired, reason = asyncio.run(main())
    # 在循环内取样（同上一条的理由：asyncio.run 退出也会取消任务）
    assert fired is True, "看门狗没留下标记，无法与用户主动停止区分"
    assert reason, "没有留下原因"


def test_watchdog_respects_progress_heartbeat():
    """★ 有进展的长任务不能被杀 —— 看门狗看的是「多久没动静」，不是总时长。

    这条区分了两种设计：
    - 错误设计：整轮硬超时 30 分钟（跑 31 分钟的合法长任务会被砍）
    - 正确设计：闲置超时（只要还有帧产出就重置计时）

    真实场景：一个跑 40 分钟、期间持续输出工具步骤的合法任务。
    """

    async def slow_but_alive(job):
        # 6 次 × 0.08s = 0.48s 总时长，远超 0.2s 阈值；
        # 但每 0.08s 有一次 publish（= 有进展），不该被杀。
        for i in range(6):
            job.publish(f"event: text_delta\ndata: {i}\n\n")
            await asyncio.sleep(0.08)

    async def main():
        job = _make_job("g_alive")
        turn_job.start_job(job, slow_but_alive, idle_timeout_s=0.2)
        await asyncio.sleep(0.9)
        return job.watchdog_fired, job.seq

    fired, seq = asyncio.run(main())
    # 在循环内取样：`watchdog_fired` 是唯一能区分「误杀」的判据。
    assert fired is False, (
        "有持续进展的 job 被误杀 —— 看门狗看的是总时长而不是闲置时长")
    assert seq == 6, f"帧数不对：{seq}"


def test_watchdog_releases_subscribers():
    """看门狗中止后，订阅者必须收到**结束信号**，否则它们会一直等。

    ⚠️ `Subscriber` 没有 `closed` 布尔字段（`__slots__` 只有
    queue / queued_bytes / overflow）—— 结束信号是往队列里塞
    `_STREAM_END` 哨兵。第一版测试断言 `sub.closed`，AttributeError。
    真实判据是「队列里出现了那个哨兵」。
    """

    async def main():
        job = _make_job("g_sub")
        sub = job.attach()
        turn_job.start_job(job, _hang, idle_timeout_s=0.1)
        await asyncio.sleep(0.4)
        # 在循环内取快照：`asyncio.run` 退出时也会调 `finish()` 塞哨兵，
        # 那会让「看门狗关了订阅者」和「事件循环退出关了订阅者」无法区分。
        fired = job.watchdog_fired
        items = []
        while not sub.queue.empty():
            items.append(sub.queue.get_nowait())
        return fired, items

    fired, items = asyncio.run(main())
    assert fired is True, "看门狗没触发（本用例的前提）"
    assert turn_job._STREAM_END in items, (
        "看门狗中止后订阅者没收到结束哨兵，SSE 连接会一直挂着")


def test_default_idle_timeout_is_reasonable():
    """默认阈值必须在合理区间（不能是 0，也不能是几小时）。

    太短会砍掉正常的慢任务（软线压缩 + MCP 冷启动实测可达 18 分钟）；
    太长则用户等不起（真实事故卡了 20 分钟没人发现）。
    """
    assert 300 <= turn_job.DEFAULT_IDLE_TIMEOUT_S <= 3600, (
        f"默认闲置超时 {turn_job.DEFAULT_IDLE_TIMEOUT_S}s 不在合理区间 "
        "（5 分钟 ~ 1 小时）")


def test_watchdog_disabled_when_zero():
    """`idle_timeout_s=0` 表示关闭看门狗（给需要无限长任务的调用方留口子）。"""

    async def slow(_job):
        await asyncio.sleep(0.05)

    async def main():
        job = _make_job("g_off")
        turn_job.start_job(job, slow, idle_timeout_s=0)
        await asyncio.sleep(0.2)
        return job.watchdog_fired, job.main_task.cancelled() if job.main_task else None

    fired, cancelled = asyncio.run(main())
    # 关闭看门狗 ⇒ 绝不标记、绝不 cancel（任务靠自然完成）
    assert fired is False, "关闭看门狗却仍触发标记"
    assert cancelled is False, "关闭看门狗却仍取消了主任务"

def test_watchdog_deferred_while_tool_in_flight():
    """★ 有工具在执行时，即使零帧也不能判卡死（对齐 CC319）。

    ## 为什么必须有这条

    一次 `code_run` 可以跑 10 分钟（`MAX_TIMEOUT_S`），期间**大概率没有任何
    输出帧** —— 工具还没返回。只看「零帧 N 秒」会把这种合法执行砍掉。

    CC319 的写法（`m0779:7994`）：

        [AsyncAgent <id>] stall watchdog deferred — N tool(s) in flight
        (toolUseIds=...)

    即：**有 tool in flight 就延迟触发，并重新排一次计时**。

    ## 判据

    - 工具在飞（`tool_in_flight > 0`）：不触发，即使闲置超过阈值
    - 工具返回后（`tool_in_flight == 0`）继续闲置：正常触发
    """

    async def hang_with_tool(job):
        job.set_tool_in_flight(1)          # 模拟 code_run 正在跑
        await asyncio.Event().wait()       # 零帧，但工具在飞

    async def main():
        job = _make_job("g_tool")
        turn_job.start_job(job, hang_with_tool, idle_timeout_s=0.1)
        await asyncio.sleep(0.6)           # 6 倍阈值
        return job.watchdog_fired, job.idle_seconds()

    fired, idle = asyncio.run(main())
    assert fired is False, (
        f"工具有 in flight 时仍被判定卡死（闲置 {idle:.2f}s）——"
        " 长 code_run 会被误杀")


def test_watchdog_fires_after_tool_returns():
    """工具返回后继续闲置，必须能正常触发（延迟 ≠ 永不触发）。"""

    async def tool_then_hang(job):
        job.set_tool_in_flight(1)
        await asyncio.sleep(0.3)           # 这段时间受保护
        job.set_tool_in_flight(0)          # 工具返回
        await asyncio.Event().wait()       # 之后零帧 → 该触发

    async def main():
        job = _make_job("g_after")
        turn_job.start_job(job, tool_then_hang, idle_timeout_s=0.15)
        await asyncio.sleep(0.9)
        return job.watchdog_fired

    fired = asyncio.run(main())
    assert fired is True, "工具返回后继续闲置却一直不触发 —— 变成永不中止"


def test_tool_in_flight_counter_is_clamped_at_zero():
    """计数不能变成负数（多减一次会让保护永久失效）。"""
    job = _make_job("g_clamp")
    job.set_tool_in_flight(1)
    job.set_tool_in_flight(0)
    job.set_tool_in_flight(0)              # 多余的递减
    assert job.tool_in_flight == 0
