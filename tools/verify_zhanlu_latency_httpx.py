"""用 httpx（与 Hlaudz 一致）严格测首字延迟，交错取样。

上一版用裸 socket + recv(65536) 按行拆，会在 chunk 边界切断 JSON 行，
既污染内容也污染计时。httpx 的 iter_lines 会正确处理分块。
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://ecloud.10086.cn/api/query/aigateway/v1"
MODEL = "moma/deepseek-latest-flash"


def load_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ZhanLu_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no key")


KEY = load_key()


def timed_run(client: httpx.Client, extra: dict, prompt: str, max_tokens: int):
    """返回 (首字 ms, 完成 ms, 正文, 思考长度, reasoning_tokens, 错误)。"""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 1.0,
        "stream": True,
        **extra,
    }
    t0 = time.perf_counter()
    first = None
    text = []
    reasoning_len = 0
    rt = None
    try:
        with client.stream("POST", f"{BASE}/chat/completions", json=payload,
                           headers={"Authorization": f"Bearer {KEY}",
                                    "Accept": "text/event-stream"}) as r:
            if r.status_code != 200:
                return None, None, "", 0, None, f"HTTP {r.status_code}"
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
                if ev.get("usage"):
                    det = (ev["usage"].get("completion_tokens_details") or {})
                    rt = det.get("reasoning_tokens")
                dl = ((ev.get("choices") or [{}])[0]).get("delta") or {}
                if dl.get("content"):
                    if first is None:
                        first = time.perf_counter() - t0
                    text.append(dl["content"])
                rc = dl.get("reasoning_content") or dl.get("reasoning")
                if rc:
                    reasoning_len += len(rc)
    except Exception as e:
        return None, None, "", 0, None, f"{type(e).__name__}: {e}"
    done = time.perf_counter() - t0
    return (first * 1000 if first else None), done * 1000, "".join(text), reasoning_len, rt, None


def main():
    prompt = "只回复两个字：好的"
    print("=" * 78)
    print(f"提示: {prompt!r}   max_tokens=100")
    print("交错取样 A(默认) / B(enable_thinking=false)，各 10 轮")
    print("=" * 78)

    a, b = [], []
    a_rt, b_rt = [], []
    a_rl, b_rl = [], []

    with httpx.Client(timeout=httpx.Timeout(200.0, connect=20.0)) as client:
        for i in range(10):
            fa, da, ta, rla, rta, ea = timed_run(client, {}, prompt, 100)
            fb, db_, tb, rlb, rtb, eb = timed_run(client, {"enable_thinking": False}, prompt, 100)
            if ea:
                print(f"  #{i+1:2d} A 失败: {ea}")
            else:
                a.append(fa)
                a_rt.append(rta)
                a_rl.append(rla)
                print(f"  #{i+1:2d} A默认    首字 {fa:7.0f} ms  总 {da:7.0f} ms  "
                      f"正文={ta!r} 思考长={rla} rt={rta}")
            if eb:
                print(f"  #{i+1:2d} B 失败: {eb}")
            else:
                b.append(fb)
                b_rt.append(rtb)
                b_rl.append(rlb)
                print(f"  #{i+1:2d} B关思考  首字 {fb:7.0f} ms  总 {db_:7.0f} ms  "
                      f"正文={tb!r} 思考长={rlb} rt={rtb}")

    print("\n" + "=" * 78)
    print("统计")
    print("=" * 78)
    for name, v, rt_list, rl_list in [
        ("默认", a, a_rt, a_rl),
        ("enable_thinking=false", b, b_rt, b_rl),
    ]:
        if not v:
            print(f"  {name}: 无有效样本")
            continue
        vs = sorted(v)
        print(f"  {name:24s} n={len(v):2d}  中位 {statistics.median(v):7.0f}  "
              f"均值 {statistics.mean(v):7.0f}  P90 {vs[int(len(vs)*0.9) - 1 if len(vs) > 1 else 0]:7.0f}  "
              f"最大 {max(v):7.0f}")
        print(f"  {'':24s} 出现思考的比例 {sum(1 for x in rl_list if x > 0)}/{len(rl_list)}  "
              f"reasoning_tokens={rt_list}")

    if a and b:
        n = min(len(a), len(b))
        wins = sum(1 for i in range(n) if a[i] > b[i])
        diffs = [a[i] - b[i] for i in range(n)]
        print(f"\n  配对比较：{wins}/{n} 轮「默认」比「关思考」慢")
        print(f"  差值中位 {statistics.median(diffs):.0f} ms")
        print(f"  中位改善 {statistics.median(a) - statistics.median(b):.0f} ms "
              f"({(statistics.median(a) - statistics.median(b)) / statistics.median(a) * 100:.0f}%)")


if __name__ == "__main__":
    main()