"""用 150k 窗口压测「自动压缩是否真的在超窗之前就触发」。

## 要回答的问题

用户提出的疑问（很合理）：

> 上限 400k，理论上不该出现 `440k / 400k`。到 90% 就该自动压缩了。

如果自动压缩真的有效，那么在**任何**时刻 `percent` 都不该逼近 100%。
本脚本把窗口压到 **150k**（软线 135k = 90%，硬线 142.5k = 95%），
这样几轮大内容就能把用量推过软线，不必真的烧 40 万 token。

## 做法

1. 建用户 + 配一个 `context_window=150000` 的模型；
2. 反复发「写长文」直到 `/context` 的 used 跨过软线，或轮数用完；
3. **每一轮前后**都读 `/context`，并抓 SSE 里的 `compaction` 事件；
4. 断言：
   - A-1 用量跨过软线之前，`percent` 始终 < 100；
   - A-2 跨过软线后，**下一轮或后台补跑**触发了压缩；
   - A-3 全程 `percent` 从未 ≥ 100（这是用户怀疑的那条）；
   - A-4 压缩后用量确实回到软线以下；
   - A-5 压缩后仍能继续对话。

## 判据必须「等」后台压缩（2026-11-15 修正）

第一版在 `c.stream()` 返回后**立刻**读 `/context` 就断言，结果 A-2/A-4 假失败：
软线压缩是赛后 fire-and-forget 的后台任务（`deferred_compact`），
脚本读数字时它还没跑完。实测同一会话几秒后 `session_history` 里就有
`_source="summarization"`，用量 136212 → 60631。

所以现在改成：断言前**轮询等待**（最多 `--wait` 秒，默认 90），
并把等待过程中观察到的每一次用量变化打印出来。判据本身没有放松——
等不到就是真失败。

## 跑法

    python tools/turn_job_verify/autocompact_150k.py --port 8107
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RUN_ID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"
# 窗口可传参。默认 150k（用户指定的值）；调小只是为了缩短真机等待时间——
# 机制是「到 90% 就该压缩」，与窗口绝对值无关，小窗口同样能证明。
WINDOW = 150_000
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  | {detail}" if detail else ""),
          flush=True)


class C:
    def __init__(self, base: str) -> None:
        self.base = base
        self.token = ""

    def _req(self, path, payload=None, method="GET", timeout=600):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data,
            headers={"content-type": "application/json",
                     **({"authorization": f"Bearer {self.token}"} if self.token else {})},
            method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode() or "{}"
            return json.loads(body) if body.strip().startswith(("{", "[")) else body

    def post(self, p, payload=None, timeout=600):
        return self._req(p, payload, "POST", timeout)

    def get(self, p, timeout=120):
        return self._req(p, None, "GET", timeout)

    def stream(self, sid: str, text: str, timeout=600) -> tuple[str, list[dict]]:
        """发一条消息，返回 (原始 SSE 文本, 解析出的事件列表)。"""
        req = urllib.request.Request(
            f"{self.base}/api/sessions/{sid}/messages",
            data=json.dumps({"text": text}).encode(),
            headers={"content-type": "application/json",
                     "accept": "text/event-stream",
                     "authorization": f"Bearer {self.token}"},
            method="POST")
        buf = b""
        with urllib.request.urlopen(req, timeout=timeout) as res:
            while True:
                chunk = res.read1(8192)
                if not chunk:
                    break
                buf += chunk
        raw = buf.decode("utf-8", errors="replace")
        events = []
        cur = ""
        for line in raw.splitlines():
            if line.startswith("event:"):
                cur = line[6:].strip()
            elif line.startswith("data:"):
                p = line[5:].strip()
                if p and p != "[DONE]":
                    try:
                        d = json.loads(p)
                        if isinstance(d, dict):
                            d.setdefault("type", cur)
                            events.append(d)
                    except json.JSONDecodeError:
                        pass
        return raw, events


def main(argv: list[str]) -> int:
    global WINDOW
    port = 8107
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if "--window" in argv:
        WINDOW = int(argv[argv.index("--window") + 1])
    base = f"http://127.0.0.1:{port}"

    print("=" * 74)
    print(f"自动压缩压测（窗口 {WINDOW}）  runId={RUN_ID}")
    print(f"  软线 = {int(WINDOW * 0.9)}（90%）  硬线 = {int(WINDOW * 0.95)}（95%）")
    print("=" * 74, flush=True)

    c = C(base)
    email = f"auto_{RUN_ID.replace('-', '_')}@t.local"
    c.token = c.post("/api/auth/register",
                     {"email": email, "password": "pw12345678",
                      "name": "AutoCompact"}).get("access_token") or ""

    from tests.e2e.harness import zhanlu_api_key
    cfg = {
        "label": "150k-测试",
        "wire_api": "openai_chat",
        "base_url": "https://ecloud.10086.cn/api/query/aigateway/v1",
        "model": "moma/deepseek-latest-flash",
        "context_window": WINDOW,          # ← 关键：压小窗口
        "max_tokens": 8192,
        "is_default": True,
        "api_key": zhanlu_api_key(),
    }
    c.post("/api/me/llm-configs", cfg)
    sid = c.post("/api/sessions", {"title": "autocompact-150k"})["id"]

    c0 = c.get(f"/api/sessions/{sid}/context")
    record("A-0 模型窗口按配置生效（150k，不是 400k）",
           c0.get("context_window") == WINDOW,
           f"window={c0.get('context_window')} "
           f"auto={c0.get('auto_compact_tokens')} hard={c0.get('hard_limit_tokens')}")

    soft = int(WINDOW * 0.9)
    samples: list[dict] = []          # 每轮结束后的 /context
    compactions: list[dict] = []      # SSE 里看到的 compaction 事件
    big_prompt = ("写一篇 12000 字的技术长文，主题自选（每次不同即可），"
                  "分 24 个小节，每节都要有独立标题和完整段落。不要用工具，直接输出。")

    crossed = False
    for turn in range(1, 31):
        raw, ev = c.stream(sid, big_prompt)
        for e in ev:
            if e.get("type") == "compaction":
                compactions.append(e)
                print(f"        ⚑ 第 {turn} 轮 SSE 收到 compaction: "
                      f"trigger={e.get('trigger')} "
                      f"{e.get('before_tokens')} → {e.get('after_tokens')}", flush=True)
        ctx = c.get(f"/api/sessions/{sid}/context")
        used, pct = ctx.get("used_tokens"), ctx.get("percent")
        samples.append({"turn": turn, "used": used, "pct": pct})
        print(f"        第 {turn} 轮后：used={used} pct={pct}%", flush=True)
        if used and used >= soft:
            crossed = True
            print(f"        ↑ 已跨过软线 {soft}", flush=True)
            break
        if turn == 30:
            break

    # 跨过软线后再发一条，验证「触发了压缩」
    before_extra = c.get(f"/api/sessions/{sid}/context")
    raw, ev = c.stream(sid, "用一句话说明刚才那几篇文章的共同主题。不要用工具。")
    for e in ev:
        if e.get("type") == "compaction":
            compactions.append(e)
            print(f"        ⚑ 跨线后那轮收到 compaction: trigger={e.get('trigger')} "
                  f"{e.get('before_tokens')} → {e.get('after_tokens')}", flush=True)
    after_extra = c.get(f"/api/sessions/{sid}/context")
    print(f"        跨线后：{before_extra.get('used_tokens')} → "
          f"{after_extra.get('used_tokens')}", flush=True)

    # 后台软线压缩是**赛后 fire-and-forget**：`done` 之后才登记任务，摘要要调一次 LLM。
    # 这里必须轮询等待，否则会把「还没来得及跑」误判成「没压缩」。
    wait_s = 90.0
    if "--wait" in argv:
        wait_s = float(argv[argv.index("--wait") + 1])
    waited = 0.0
    settled = after_extra
    trail: list[int] = [int(after_extra.get("used_tokens") or 0)]
    while waited < wait_s:
        if (settled.get("used_tokens") or 0) < soft:
            break
        time.sleep(5)
        waited += 5
        settled = c.get(f"/api/sessions/{sid}/context")
        used_now = int(settled.get("used_tokens") or 0)
        if used_now != trail[-1]:
            trail.append(used_now)
            print(f"        等待压缩 {waited:.0f}s：used={used_now}", flush=True)
    if settled is not after_extra:
        print(f"        压缩结算：{wait_s:.0f}s 内 {trail[0]} → {trail[-1]}"
              f"（等了 {waited:.0f}s）", flush=True)

    # ---------------- 断言 ----------------
    pre_cross = [s for s in samples if (s["used"] or 0) < soft]
    record("A-1 跨软线之前 percent 始终 < 100",
           all((s["pct"] or 0) < 100 for s in pre_cross),
           f"{len(pre_cross)} 个样本，最大 pct="
           f"{max([s['pct'] or 0 for s in pre_cross], default=0)}%")

    all_pct = [s["pct"] or 0 for s in samples] + [settled.get("percent") or 0]
    record("A-3 全程 percent 从未 ≥ 100（用户怀疑的那条）",
           max(all_pct) < 100, f"最大 {max(all_pct)}%")

    compacted = bool(compactions) or (
        (settled.get("used_tokens") or 0) < (before_extra.get("used_tokens") or 0))
    record("A-2 跨过软线后触发了压缩", crossed and compacted,
           f"跨线={crossed}；compaction 事件 {len(compactions)} 次；"
           f"used {before_extra.get('used_tokens')} → {settled.get('used_tokens')}"
           f"（等待 {waited:.0f}s）")

    record("A-4 压缩后用量回到软线以下",
           (settled.get("used_tokens") or 0) < soft,
           f"used={settled.get('used_tokens')} < soft={soft}")

    ok_send, err = True, ""
    try:
        c.stream(sid, "再来一句：CAP 的 P 指什么？一句话。")
    except Exception as e:  # noqa: BLE001
        ok_send, err = False, f"{type(e).__name__}: {e}"
    record("A-5 压缩后仍能继续对话", ok_send, err or "成功")

    failed = [n for n, ok, _ in results if not ok]
    print()
    print(f"通过 {len(results) - len(failed)} / 失败 {len(failed)}")
    for n in failed:
        print(f"  ✗ {n}")

    out = ROOT / "backend" / "tests" / "e2e" / "runs" / f"{RUN_ID}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(
        {"run_id": RUN_ID, "session_id": sid, "window": WINDOW, "soft": soft,
         "samples": samples, "compactions": compactions,
         "before_extra": before_extra, "after_extra": after_extra,
         "settled": settled, "waited_s": waited, "used_trail": trail,
         "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"现场：{out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
