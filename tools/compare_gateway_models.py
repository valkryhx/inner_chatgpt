"""横向对比网关各模型的首字延迟稳定性。

核心问题：湛卢网关的延迟抖动（59ms~18s）是「所有模型都这样」还是
「只有 deepseek 这条线这样」。

判据不能只看中位——真正让人"卡"的是 P90/最大值。所以每个模型取 8 次，
报告 中位 / P90 / 最大 / 失败数。
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://ecloud.10086.cn/api/query/aigateway/v1"

CANDIDATES = [
    "moma/deepseek-latest-flash",   # 当前在用
    "moma/qwen3.7-max",
    "moma/qwen3.7-plus",
    "moma/qwen3.6-plus",
    "moma/glm-5.3",
    "moma/glm-5.3-flash",
    "moma/glm-4.7",
    "moma/kimi-k3",
    "moma/minimax-m3",
    "zhanlu/qwen3.7-max",
    "zhanlu/glm-5.3",
]

PROMPT = "只回复两个字：好的"
ROUNDS = 8


def load_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ZhanLu_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no key")


KEY = load_key()


def once(client: httpx.Client, model: str) -> tuple[float | None, str, str]:
    """返回 (首字 ms, 正文, 错误)。"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 50,
        "temperature": 1.0,
        "stream": True,
    }
    t0 = time.perf_counter()
    first = None
    text = []
    try:
        with client.stream("POST", f"{BASE}/chat/completions", json=payload,
                           headers={"Authorization": f"Bearer {KEY}",
                                    "Accept": "text/event-stream"}) as r:
            if r.status_code != 200:
                err = r.read()[:160].decode("utf-8", "replace")
                return None, "", f"HTTP {r.status_code}: {err}"
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                p = line[5:].strip()
                if p in ("", "[DONE]"):
                    continue
                try:
                    ev = json.loads(p)
                except Exception:
                    continue
                if ev.get("error"):
                    return None, "", f"SSE error: {json.dumps(ev['error'], ensure_ascii=False)[:120]}"
                dl = ((ev.get("choices") or [{}])[0]).get("delta") or {}
                if dl.get("content"):
                    if first is None:
                        first = time.perf_counter() - t0
                    text.append(dl["content"])
    except Exception as e:
        return None, "", f"{type(e).__name__}"
    return (first * 1000 if first else None), "".join(text), ""


def main():
    print(f"每个模型 {ROUNDS} 次，交错轮转（不是测完一个再测下一个，避免时段偏差）")
    print(f"提示: {PROMPT!r}\n")

    samples: dict[str, list[float]] = {m: [] for m in CANDIDATES}
    errors: dict[str, list[str]] = {m: [] for m in CANDIDATES}
    replies: dict[str, str] = {}

    with httpx.Client(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
        for rnd in range(ROUNDS):
            for m in CANDIDATES:
                ms, text, err = once(client, m)
                if err:
                    errors[m].append(err)
                elif ms:
                    samples[m].append(ms)
                    replies.setdefault(m, text)
            done = rnd + 1
            print(f"  ... 第 {done}/{ROUNDS} 轮完成", flush=True)

    print("\n" + "=" * 92)
    print(f"{'模型':<28}{'n':>3}{'中位':>9}{'均值':>9}{'P90':>9}{'最大':>9}{'失败':>5}  回复")
    print("=" * 92)
    rows = []
    for m in CANDIDATES:
        v = sorted(samples[m])
        if not v:
            print(f"{m:<28}{0:>3}{'-':>9}{'-':>9}{'-':>9}{'-':>9}{len(errors[m]):>5}  "
                  f"{(errors[m][0] if errors[m] else '')[:30]}")
            continue
        p90 = v[min(len(v) - 1, int(len(v) * 0.9))]
        rows.append((statistics.median(v), m, v, p90))
        print(f"{m:<28}{len(v):>3}{statistics.median(v):>9.0f}"
              f"{statistics.mean(v):>9.0f}{p90:>9.0f}{max(v):>9.0f}"
              f"{len(errors[m]):>5}  {replies.get(m, '')[:10]!r}")

    print("\n" + "=" * 92)
    print("按「中位延迟」排序（最稳的在前）")
    print("=" * 92)
    for p50, m, v, p90 in sorted(rows):
        # 抖动比 = P90 / 中位；越大说明越不稳
        jitter = p90 / p50 if p50 else 0
        flag = "  << 当前在用" if "deepseek-latest-flash" in m else ""
        print(f"  {m:<28} 中位 {p50:7.0f} ms  P90 {p90:7.0f} ms  抖动比 {jitter:4.1f}x{flag}")

    print("\n说明：抖动比 = P90/中位。网关排队导致的长尾才是「卡」的主因，")
    print("      中位低但抖动比高的模型，体验依然会忽快忽慢。")


if __name__ == "__main__":
    main()