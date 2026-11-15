"""进程内探针：生成仍在跑时，`has_active_generation()` 会不会返回 False？

## 为什么需要它

`stale_raw.py`（直读原始列、不发任何 HTTP 读）证明：**没有 API 读就没有
stopped 写入**。而 `stale_probe.py`（每 2 秒读一次 `GET /api/sessions/{sid}`）
在 58.7s 观测到 stopped，随后 62.8s 又回到 streaming —— 那一轮显然还活着。

唯一的写入者是 `routers/sessions.py: get_session` 的自愈分支：

```python
if not has_active_generation(session_id, user["id"]):
    await db.mark_stale_streaming_stopped(session_id)   # 把 streaming 改 stopped
```

所以要问的是：**那一轮还活着，为什么判据是 False？**

## 本探针验什么

不经过 HTTP，直接在进程内用真的 `turn_job` + 真的 `runtime` 注册表：
起一个「长时间不出帧」的 job（模拟 `code_run` 睡 60 秒那段静默），
然后按秒轮询 `has_active_generation()`。

判据：

- I-1 起完 job 后立刻判活跃 → True（基线）；
- I-2 静默期内**全程**判活跃 → True。任何一次 False 就是缺陷：
  它会让任意一次「刷新页面 / 读会话详情」把正在跑的轮次标成 stopped；
- I-3 job 结束后判活跃 → False（收口正确）。

## 跑法

    cd backend && python ../tools/turn_job_verify/active_probe.py
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RUN_ID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"


def main() -> int:
    from app import runtime, turn_job

    user_id = f"u_probe_{RUN_ID.replace('-', '_')}"
    session_id = f"co_probe_{RUN_ID.replace('-', '_')}"
    gen = f"g_probe_{RUN_ID}"

    print("=" * 72)
    print(f"活跃判据探针  runId={RUN_ID}")
    print("=" * 72, flush=True)

    async def scenario() -> tuple[list[tuple[float, bool]], float]:
        job = turn_job.TurnJob(user_id, session_id, gen, "msg_probe")
        sub = job.attach()

        async def _run_job(j: turn_job.TurnJob) -> None:
            # 与 routers/chat.py 的 _run_job 同构：在 job 任务内部登记。
            stop_event = runtime.register_generation(user_id, session_id, gen)
            try:
                j.publish("event: message_start\ndata: {}\n\n")
                # 模拟 code_run 长静默：40 秒不出任何帧
                await asyncio.sleep(40)
                j.publish("event: done\ndata: {}\n\n")
            finally:
                runtime.unregister_generation(user_id, session_id, gen)

        # 消费订阅者，避免队列堆积（不消费也不影响判据，但更贴近真实）
        async def _drain() -> None:
            async for _chunk in sub.drain():
                pass

        turn_job.start_job(job, _run_job)
        drain_task = asyncio.create_task(_drain())

        samples: list[tuple[float, bool]] = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < 44:
            await asyncio.sleep(1.0)
            t = round(time.monotonic() - t0, 1)
            samples.append((t, runtime.has_active_generation(session_id, user_id)))
        await drain_task
        return samples, time.monotonic() - t0

    samples, _elapsed = asyncio.run(scenario())

    for t, live in samples:
        print(f"  {t:>6.1f}s  has_active_generation={live}")

    during = [live for t, live in samples if 1.0 <= t <= 38.0]
    after = [live for t, live in samples if t >= 42.0]

    ok1 = bool(samples) and samples[0][1] is True
    ok2 = bool(during) and all(during)
    ok3 = bool(after) and not any(after)

    print()
    print(f"I-1 起完 job 后立刻判活跃                     : {'OK' if ok1 else 'FAIL'}")
    print(f"I-2 静默期内**全程**判活跃                    : {'OK' if ok2 else 'FAIL'}"
          + ("" if ok2 else f"（{sum(1 for x in during if not x)}/{len(during)} 次为 False）"))
    print(f"I-3 job 结束后判不活跃                        : {'OK' if ok3 else 'FAIL'}")

    if not ok2:
        first_false = next(t for t, live in samples if 1.0 <= t <= 38.0 and not live)
        print(f"\n**缺陷 confirmed**：静默期第 {first_false}s 起判据变 False。")
        print("  后果：任何一次 GET /api/sessions/{sid}（前端 loadMessages）都会把")
        print("  正在跑的这一轮标成 stopped，随后 job 的下一次 _persist 又写回")
        print("  streaming —— 前端看到状态来回翻转。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
