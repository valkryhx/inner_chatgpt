"""探针：生成进行中，`GET /api/sessions/{sid}` 会不会把消息误标成 stopped？

## 为什么要这条探针

2026-11-15 真机跑 B 场景（Team 任务 + 中断续看）时出现一组自相矛盾的现象：

```
B-4 最终落库终态 complete  | 实测 status=stopped     ← 我的轮询那一刻读到 stopped
B-8 主答复里复述了哨兵      | 实测 正文长度=5          ← 当时正文只有 5 字
```

而**同一批次跑完后**再查库，那条 assistant 是 `complete`、正文 975 字、子代理卡片收口。
即：中途被人写成了 `stopped`，之后那一轮又跑完并覆盖成 `complete`。

后端访问日志里**没有**该会话的 `POST /stop`，也没有 error/warn。
唯一会在没有任何用户停止的情况下把 `streaming` 改成 `stopped` 的，是
`routers/sessions.py: get_session` 里的「自愈」分支：

```python
if not has_active_generation(session_id, user["id"]):
    ... converge_generation_facts(..., status="stale")
    await db.mark_stale_streaming_stopped(session_id)
```

也就是说：只要**进程内活跃表**那一刻看起来是空的，一次普通的「读会话详情」
就会把正在跑的一轮标成 stopped。而前端刷新页面、`loadMessages()` 走的正是这个端点。

## 判据（三条，缺一不可）

1. **P-1** 探针发出的那一轮在整段观测期内**没有** `POST /stop`（自证：只发一次消息）；
2. **P-2** 观测期内确实看到了 streaming 状态（否则可能是轮次已经结束，测不到）；
3. **P-3** 观测期内状态**翻转成 stopped**，且之后 SSE 仍在产出帧
   —— 翻转时刻那一轮显然还活着，所以这是**误标**，不是正常停止。

只要 P-3 成立，就是产品缺陷（误标），不是测试判据问题。

## 跑法

    python tools/turn_job_verify/stale_probe.py --base http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "turn_job_verify"))
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from live_selftest import Client, get_messages, parse_sse_frames, stream_get, stream_post  # noqa: E402

RUN_ID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}-stale"

# 关键：这一段 **静默很久**（code_run 睡 60 秒），期间没有任何 SSE 帧，
# 正好模拟「长任务进行中，用户刷新页面 / 前端轮询」的窗口。
PROMPT = (
    "请调用 code_run 工具执行下面这段 Python，然后把输出原样告诉我，不要做别的事：\n"
    "```python\nimport time\nprint('start', flush=True)\ntime.sleep(60)\n"
    "print('end', flush=True)\n```"
)


def status_of(client: Client, sid: str) -> str:
    msgs = get_messages(client, sid)
    last = next((m for m in reversed(msgs) if m.get("role") == "assistant"), None)
    return (last or {}).get("status", "none")


def main(argv: list[str]) -> int:
    base = "http://127.0.0.1:8000"
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]

    print("=" * 72)
    print(f"stale 探针  runId={RUN_ID}  base={base}")
    print("=" * 72, flush=True)

    client = Client(base)
    client.setup_user()
    sid = client.new_session("stale-probe")
    gen = f"g-stale-{RUN_ID}"

    # 后台线程跑 SSE（只观测，不停止）
    seen = {"frames": 0, "last_frame_at": 0.0, "done": False}
    t0 = time.monotonic()

    def sse_worker() -> None:
        def on_chunk(chunk: str, t: float) -> None:
            if "event:" in chunk or "data:" in chunk:
                seen["frames"] += 1
                seen["last_frame_at"] = t
        try:
            stream_post(client, sid, PROMPT, gen, on_chunk=on_chunk, stop_after_s=95.0)
        finally:
            seen["done"] = True

    th = threading.Thread(target=sse_worker, daemon=True)
    th.start()

    # 主线程：每 2 秒读一次会话详情（就是前端的 loadMessages 路径）
    timeline: list[tuple[float, str, int]] = []
    flip_at: float | None = None
    while time.monotonic() - t0 < 92:
        time.sleep(2.0)
        t = round(time.monotonic() - t0, 1)
        st = status_of(client, sid)
        timeline.append((t, st, seen["frames"]))
        if st == "streaming":
            pass
        elif st == "stopped" and flip_at is None and any(s == "streaming" for _, s, _ in timeline):
            flip_at = t
        if seen["done"] and st in ("complete", "stopped", "error") and t > 5:
            break

    saw_streaming = any(s == "streaming" for _, s, _ in timeline)
    frames_after_flip = seen["frames"] - next(
        (f for tt, s, f in timeline if tt == flip_at), seen["frames"]) if flip_at else 0

    print()
    print("时间线（t, db 里的 assistant.status, 当时已收到的 SSE 帧数）：")
    for t, s, f in timeline:
        print(f"  {t:>6.1f}s  {s:<10} frames={f}")

    ok1 = True  # 全程只发了一条消息、没调 /stop —— 由本探针构造保证
    ok2 = saw_streaming
    ok3 = flip_at is not None and frames_after_flip > 0

    print()
    print(f"P-1 全程没有 POST /stop（构造保证）           : {'OK' if ok1 else 'FAIL'}")
    print(f"P-2 观测期内看到过 streaming                  : {'OK' if ok2 else 'FAIL'}")
    print(f"P-3 状态翻成 stopped 且之后 SSE 仍在出帧     : {'OK' if ok3 else 'FAIL'}"
          + (f"（翻转于 {flip_at}s，之后又收到 {frames_after_flip} 帧）" if flip_at else "（未翻转）"))
    print()
    print("结论：" + ("**误标 confirmed** —— 普通的读会话详情就会把正在跑的轮次标成 stopped"
                    if ok3 else "未复现"))

    out = ROOT / "backend" / "tests" / "e2e" / "runs" / f"{RUN_ID}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(
        {"run_id": RUN_ID, "session_id": sid, "timeline": timeline,
         "flip_at": flip_at, "frames_after_flip": frames_after_flip,
         "P1": ok1, "P2": ok2, "P3": ok3,
         "final_status": status_of(client, sid)},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"现场：{out}")
    return 0 if ok3 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
