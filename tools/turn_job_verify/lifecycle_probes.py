"""turn_job 生命周期探针：四条与 CPython 任务/事件循环行为有关的实证。

## 为什么需要它

`turn_job.py` 里有两处修复依赖「CPython 具体怎么行为」的知识，而这些知识
**不能靠读文档断言**，只能实测：

1. 协程体一行都没执行就被 `cancel()` 时，`finally` 不跑 —— 所以光靠
   `_wrapped` 的 finally 收口会漏，必须有 done_callback 兜底；
2. `asyncio.wait_for(queue.get(), timeout)` 超时取消的是外层等待，
   队列项留在原位 —— 所以心跳插入不会丢真实帧；
3. 同一会话真的会出现「两个未 finish 的 job」的窗口。

任何一条假设变了（Python 版本升级、事件循环实现变化），对应修复就该重新审视。
这个脚本就是把这三条假设固化成可复跑的断言。

## 跑法

    python tools/turn_job_verify/lifecycle_probes.py

全部 PASS 退出码 0；任何一条 FAIL 退出码 1。
"""
from __future__ import annotations

import asyncio
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import turn_job  # noqa: E402

results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {name}" + (f"  | {detail}" if detail else ""))


# ---------------------------------------------------------------- 假设 1

def probe_finally_on_prestart_cancel() -> None:
    """协程体未开始就 cancel → finally 不跑。这是 done_callback 兜底的存在理由。"""
    log: list[str] = []

    async def worker() -> None:
        try:
            log.append("body")
            await asyncio.sleep(3600)
        finally:
            log.append("finally")

    async def main() -> None:
        # A：已开始跑再取消
        a = asyncio.create_task(worker())
        await asyncio.sleep(0)
        a.cancel()
        try:
            await a
        except asyncio.CancelledError:
            pass
        started_case = list(log)

        log.clear()
        # B：创建后立刻取消，一个 await 点都没过
        b = asyncio.create_task(worker())
        b.cancel()
        try:
            await b
        except asyncio.CancelledError:
            pass
        prestart_case = list(log)

        record("P1a 已开始后 cancel → finally 会跑",
               started_case == ["body", "finally"], f"{started_case}")
        record("P1b 未开始就 cancel → finally **不跑**",
               prestart_case == [],
               f"{prestart_case}（正因如此才需要 done_callback 兜底）")

    asyncio.run(main())


def probe_done_callback_fires() -> None:
    """done_callback 在「未启动即取消」时仍触发 —— 所以它能当兜底收口。"""
    fired: list[str] = []

    async def body() -> None:
        fired.append("body")
        await asyncio.sleep(3600)

    async def main() -> None:
        t = asyncio.create_task(body())
        t.add_done_callback(lambda task: fired.append(f"done(cancelled={task.cancelled()})"))
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        record("P2 done_callback 在未启动即取消时仍触发",
               any(f.startswith("done(cancelled=True)") for f in fired), f"{fired}")

    asyncio.run(main())


def probe_zombie_job() -> None:
    """未启动即取消的 job 必须被摘出 _JOBS，且订阅者能干净退出（不挂死）。"""
    async def main() -> None:
        async def runner(j: turn_job.TurnJob) -> None:
            for i in range(100):
                await asyncio.sleep(0.01)
                j.publish(f"frame{i}")

        job = turn_job.TurnJob("u1", "s1", "gzombie")
        sub = job.attach()
        turn_job.start_job(job, runner)
        job.task.cancel()                 # 不给事件循环跑协程体的机会
        try:
            await job.task
        except asyncio.CancelledError:
            pass

        leaked = turn_job._key("u1", "s1", "gzombie") in turn_job._JOBS
        record("P3a job 未启动即取消后不留在表里", not leaked and job.finished,
               f"finished={job.finished}")

        try:
            frames = await asyncio.wait_for(_drain(sub), timeout=2.0)
            record("P3b 该 job 的订阅者能干净退出", frames == [], f"{frames}")
        except asyncio.TimeoutError:
            record("P3b 该 job 的订阅者能干净退出", False,
                   "drain 超时 —— 永久挂住（这正是未修复时的表现）")

        turn_job._JOBS.clear()

    asyncio.run(main())


async def _drain(sub) -> list[str]:
    return [f async for f in sub.drain()]


# ---------------------------------------------------------------- 假设 2

def probe_wait_for_keeps_queue_item() -> None:
    """`wait_for(queue.get(), timeout)` 超时不丢队列项（心跳插入的前提）。

    30 轮 × 200 帧、超时压到 1ms（几乎每帧都撞超时）。任一轮丢帧即 FAIL。
    """
    async def once(n_items: int, timeout: float, seed: int) -> int:
        random.seed(seed)
        q: asyncio.Queue = asyncio.Queue()
        got = 0

        async def producer() -> None:
            for i in range(n_items):
                await asyncio.sleep(random.uniform(0, timeout * 0.6))
                q.put_nowait(i)
            q.put_nowait(None)

        async def consumer() -> None:
            nonlocal got
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout)
                except asyncio.TimeoutError:
                    continue                     # 心跳分支
                if item is None:
                    return
                got += 1

        await asyncio.gather(producer(), consumer())
        return got

    async def main() -> None:
        bad = [s for s in range(30) if await once(200, 0.001, s) != 200]
        record("P4 wait_for 超时不丢队列项（30 轮 × 200 帧）",
               not bad, f"丢帧的轮次：{bad}" if bad else "0 轮丢帧")

    asyncio.run(main())


# ---------------------------------------------------------------- 假设 3

def probe_two_live_jobs_window() -> None:
    """同一会话能短暂出现两个未 finish 的 job，且查找必须挑最新一代。

    真实次序（`routers/chat.py`）：
        `_event_stream` finally → await release_generation()   ← DB 租约此刻已空
        `_run_job` finally      → unregister / 清 reservation
        `_wrapped` finally      → job.finish() 摘出 _JOBS
    中间那段 await 里新请求已经能抢到租约并建新 job。
    """
    released = asyncio.Event()

    async def old_runner(j: turn_job.TurnJob) -> None:
        try:
            await asyncio.sleep(10)
        finally:
            released.set()
            await asyncio.sleep(0.05)    # 模拟 release_generation 的 await 窗口

    async def hang_runner(j: turn_job.TurnJob) -> None:
        await asyncio.sleep(10)

    async def main() -> None:
        old = turn_job.TurnJob("u1", "s1", "g-old")
        turn_job.start_job(old, old_runner)
        await asyncio.sleep(0.01)

        old.task.cancel()                # 模拟 /stop
        await released.wait()            # 进入「租约已释放、job 还没 finish」

        new = turn_job.TurnJob("u1", "s1", "g-new")
        turn_job.start_job(new, hang_runner)

        keys = sorted(k[2] for k in turn_job._JOBS)
        picked = turn_job.get_live_job_for_session("u1", "s1")
        got = picked.generation_id if picked else None
        record("P5a 同一会话确实能有两个未 finish 的 job",
               keys == ["g-new", "g-old"], f"_JOBS={keys}")
        record("P5b 查找取最新一代（不是第一个扫到的）",
               got == "g-new", f"取到 {got}，应为 g-new")

        for t in (old.task, new.task):
            if t and not t.done():
                t.cancel()
        await asyncio.sleep(0.05)
        turn_job._JOBS.clear()

    asyncio.run(main())


def main() -> int:
    print("=" * 72)
    print("turn_job 生命周期探针")
    print("=" * 72)
    probe_finally_on_prestart_cancel()
    probe_done_callback_fires()
    probe_zombie_job()
    probe_wait_for_keeps_queue_item()
    probe_two_live_jobs_window()

    failed = [n for n, ok, _ in results if not ok]
    print()
    print("=" * 72)
    print(f"通过 {len(results) - len(failed)} / 失败 {len(failed)}")
    for n in failed:
        print(f"  ✗ {n}")
    print("=" * 72)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
