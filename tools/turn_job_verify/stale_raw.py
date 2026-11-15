"""决定性探针：绕开 API，直接读 SQLite 原始 status 列。

## 为什么前两个探针不算数

`stale_probe.py` 与 `stale_probe2.py` 都通过 **HTTP API** 读状态，而
`GET /api/sessions/{sid}`（`routers/sessions.py: get_session`）本身就带一段
「自愈」写入：

```python
if not has_active_generation(session_id, user["id"]):
    await team_store.converge_generation_facts(..., status="stale")
    await db.mark_stale_streaming_stopped(session_id)     # ← 会改 status
```

**观测者即疑似写入者** —— 两个探针的结果因此互相矛盾（probe1 前 56s streaming、
probe2 从 t=2 就 stopped）。这正是本项目反复吃的「探针伪造现象」那一类错误。

## 本探针的做法

用独立的 sqlite3 连接**直读 `messages.status` 原始列**，全程不发任何 HTTP 读请求；
唯一的 HTTP 只有那一次 `POST /messages` 开轮。于是：

- 原始列若**从不**变 stopped → 翻转由 API 自愈造成（观测者自己写的）；
- 原始列若**也**变 stopped → 另有写入者，需继续追。

WAL 模式允许多读者并发，外部只读连接不会干扰后端。

## 判据

- R-1 观测期内原始列出现过 `streaming`（样本有效）；
- R-2 原始列出现过 `stopped`，且之后又回到 `streaming` 或最终 `complete`
  → **DB 层被第三方改写**（job 自己只会写 streaming/终态，不会写 stopped 再写回来）；
- R-3 全程原始列只有 `streaming` → `complete` → **API 自愈是唯一写入者**。

R-2 与 R-3 都指向缺陷，区别只在写入者身份。

## 跑法

    python tools/turn_job_verify/stale_raw.py --base http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "turn_job_verify"))
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from live_selftest import Client, stream_post  # noqa: E402

RUN_ID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}-raw"
DB = Path(os.environ.get("HLZ_PROBE_DB") or (ROOT / "backend" / "data" / "app.db"))

PROMPT = (
    "请调用 code_run 工具执行下面这段 Python，然后把输出原样告诉我，不要做别的事：\n"
    "```python\nimport time\nprint('start', flush=True)\ntime.sleep(60)\n"
    "print('end', flush=True)\n```"
)


def raw_status(sid: str) -> tuple[str, str]:
    """绕开 API 直读原始列：返回 (assistant 的 status, 消息 id)。"""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    try:
        row = con.execute(
            "select status, id from messages where session_id=? and role='assistant' "
            "order by created_at desc, rowid desc limit 1", (sid,)
        ).fetchone()
    finally:
        con.close()
    return (row[0], row[1]) if row else ("none", "")


def main(argv: list[str]) -> int:
    base = "http://127.0.0.1:8000"
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]

    print("=" * 72)
    print(f"原始列探针  runId={RUN_ID}  base={base}")
    print(f"DB(只读) = {DB}")
    print("=" * 72, flush=True)

    client = Client(base)
    client.setup_user()
    sid = client.new_session("stale-raw")
    gen = f"g-raw-{RUN_ID}"

    seen = {"frames": 0}
    t0 = time.monotonic()

    def sse_worker() -> None:
        def on_chunk(chunk: str, t: float) -> None:
            if "event:" in chunk or "data:" in chunk:
                seen["frames"] += 1
        try:
            stream_post(client, sid, PROMPT, gen, on_chunk=on_chunk, stop_after_s=95.0)
        except Exception:
            pass

    threading.Thread(target=sse_worker, daemon=True).start()

    timeline: list[tuple[float, str, int]] = []
    while time.monotonic() - t0 < 92:
        time.sleep(2.0)
        t = round(time.monotonic() - t0, 1)
        st, _mid = raw_status(sid)
        timeline.append((t, st, seen["frames"]))
        if st in ("complete", "error") and t > 5:
            break

    for t, s, f in timeline:
        print(f"  {t:>6.1f}s  {s:<10} frames={f}")

    states = [s for _t, s, _f in timeline]
    saw_streaming = "streaming" in states
    # 出现过 stopped 后又有非 stopped 的状态 → 有人先写 stopped 再被别人覆盖
    rev = None
    for i, s in enumerate(states):
        if s == "stopped":
            after = states[i + 1:]
            if any(x in ("streaming", "complete") for x in after):
                rev = timeline[i][0]
            break

    print()
    print(f"R-1 原始列出现过 streaming                     : {'OK' if saw_streaming else 'FAIL'}")
    if rev is not None:
        print(f"R-2 原始列出现 stopped 之后又恢复（第三方改写）: OK（{rev}s）")
        verdict = "DB 层被第三方改写"
    elif "stopped" in states:
        print("R-2 原始列出现 stopped 之后又恢复           : FAIL（stopped 后未恢复）")
        verdict = "stopped 后未恢复"
    else:
        print("R-3 原始列只有 streaming → 终态                : OK")
        verdict = "API 自愈是唯一写入者（直读不受影响）"

    print(f"结论：{verdict}")

    out = ROOT / "backend" / "tests" / "e2e" / "runs" / RUN_ID
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(
        {"run_id": RUN_ID, "session_id": sid, "timeline": timeline,
         "saw_streaming": saw_streaming, "recovered_after": rev, "verdict": verdict},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"现场：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
