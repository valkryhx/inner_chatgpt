"""生产默认心跳（15s）实测：确认 `.env` 里的值真的生效，且是「静默才发」。

## 为什么要单独一条

第一轮 `live_selftest V-3/V-3b` 是在**启动器**环境下跑的（心跳压到 4s），
它证明的是「机制按环境变量工作」。而 8000 端口这一轮用的是 `.env` 里的
**生产默认 15s** —— 这中间隔着一次「环境变量到底有没有被 dotenv 读进进程」，
不验就是假设。

## 判据

1. 一次 `code_run` 睡 40 秒的静默期里，心跳**必须出现**且间隔≈15s；
2. 心跳只出现在静默窗口 —— 正文流动期间不该有。
   这条用「心跳时刻 vs 首个非心跳帧」的时间关系判定，不看总数。

## 跑法

    python tools/turn_job_verify/heartbeat_prod.py --base http://127.0.0.1:8000
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "turn_job_verify"))
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from live_selftest import Client, stream_post  # noqa: E402

RUN_ID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}-hb"
results: list[tuple[str, bool, str]] = []

LONG_PROMPT = ("请调用 code_run 工具执行下面这段 Python，然后把输出原样告诉我，"
               "不要做别的事：\n```python\nimport time\nprint('start', flush=True)\n"
               "time.sleep(40)\nprint('end', flush=True)\n```")


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  | {detail}" if detail else ""), flush=True)


def main(argv: list[str]) -> int:
    base = "http://127.0.0.1:8000"
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]

    print("=" * 72)
    print(f"生产默认心跳实测  runId={RUN_ID}  base={base}")
    print("=" * 72, flush=True)

    client = Client(base)
    client.setup_user()
    sid = client.new_session("hb-prod")
    gen = f"g-hb-{RUN_ID}"

    hbs: list[float] = []
    other: list[float] = []
    t0 = time.monotonic()

    def on_chunk(chunk: str, t: float) -> None:
        nonlocal t0
        for line in chunk.split("\n"):
            if line.startswith(":"):
                hbs.append(t)
            elif line.startswith(("event:", "data:")):
                other.append(t)

    stream_post(client, sid, LONG_PROMPT, gen, on_chunk=on_chunk, stop_after_s=45.0)
    try:
        client.post(f"/api/sessions/{sid}/stop", {"generation_id": gen})
    except Exception:
        pass

    record("H-1 静默期收到心跳（说明 .env 的 15s 真的生效了）",
           len(hbs) >= 1,
           f"{len(hbs)} 帧，时刻={[round(x, 1) for x in hbs[:8]]}")

    if len(hbs) >= 2:
        gaps = [round(hbs[i + 1] - hbs[i], 1) for i in range(len(hbs) - 1)]
        ok = all(11.0 <= g <= 20.0 for g in gaps)
        record("H-2 心跳间隔≈15s（±5s 容差）", ok, f"间隔={gaps[:8]}")
    elif len(hbs) == 1:
        record("H-2 心跳间隔≈15s", False, "只收到 1 帧，间隔无法判定")

    if hbs:
        first_hb = hbs[0]
        # 静默期的起点：最后一个非心跳帧的时刻
        last_event = max([t for t in other if t <= first_hb], default=0.0)
        quiet = round(first_hb - last_event, 1)
        record("H-3 首帧心跳发生在真正静默之后（≥12s，不是固定 ticker）",
               12.0 <= quiet <= 22.0, f"距上一个事件帧 {quiet}s")
    else:
        record("H-3 首帧心跳发生在真正静默之后", False, "没收到心跳")

    failed = [n for n, ok, _ in results if not ok]
    print()
    print(f"通过 {len(results) - len(failed)} / 失败 {len(failed)}")
    for n in failed:
        print(f"  ✗ {n}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
