"""超窗窗口期行为：用量显示 >100% 时，用户还能不能正常发消息？

## 背景（2026-11-15 实测）

`autocompact_150k.py` 用 30k 窗口实测到：

```
第 3 轮后：used=26352  pct=87.84%
第 4 轮后：used=36427  pct=121.42%   ← 跨过软线，且超过 100%
⚑ 跨线后那轮收到 compaction: trigger=hard_trim 36427 → 10100
```

根因（`routers/chat.py` 注释写明）：软线压缩**默认赛后跑**
（`HLZ_SOFT_COMPACT_DEFER` 默认开），为的是不让摘要的几秒推迟首个 token。
于是「一轮结束后 → 后台压缩完成前」存在一个窗口，里面 `/context` 会读到
超过软线、甚至超过 100% 的值。

**这本身是不是缺陷，取决于用户在这个窗口里能不能继续用。**
如果下一轮开头有硬线兜底（`enforce_hard_limit`）把历史裁到硬线以下，
用户就只是看到一个吓人的百分比，功能不受影响；否则就是真卡死。

## 本脚本验什么

在窗口期（pct > 100）立刻发下一条消息，看：

- W-1 消息发送成功（不 4xx/5xx、不超时）；
- W-2 这一轮**收到了响应内容**（不是空回复）；
- W-3 随后用量回到软线以下（硬线兜底或后台压缩生效）；
- W-4 全程没有把历史抹空（压缩后仍有消息）。

## 跑法

    python tools/turn_job_verify/overwindow_behavior.py --port 8117 --window 30000
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

    def stream(self, sid: str, text: str, timeout=600):
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
        events, cur = [], ""
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
    port = 8117
    window = 30000
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if "--window" in argv:
        window = int(argv[argv.index("--window") + 1])
    base = f"http://127.0.0.1:{port}"
    soft = int(window * 0.9)

    print("=" * 74)
    print(f"超窗窗口期行为  runId={RUN_ID}  窗口={window} 软线={soft}")
    print("=" * 74, flush=True)

    c = C(base)
    email = f"over_{RUN_ID.replace('-', '_')}@t.local"
    c.token = c.post("/api/auth/register",
                     {"email": email, "password": "pw12345678",
                      "name": "OverWindow"}).get("access_token") or ""
    from tests.e2e.harness import zhanlu_api_key
    c.post("/api/me/llm-configs", {
        "label": "overwindow", "wire_api": "openai_chat",
        "base_url": "https://ecloud.10086.cn/api/query/aigateway/v1",
        "model": "moma/deepseek-latest-flash",
        "context_window": window, "max_tokens": 8192,
        "is_default": True, "api_key": zhanlu_api_key(),
    })
    sid = c.post("/api/sessions", {"title": "overwindow"})["id"]

    # 1) 充到超过软线（每轮写长文）
    big = ("写一篇 12000 字的技术长文，主题自选，分 24 个小节，"
           "每节独立标题和完整段落。不要用工具。")
    over = None
    for turn in range(1, 12):
        c.stream(sid, big)
        ctx = c.get(f"/api/sessions/{sid}/context")
        used, pct = ctx.get("used_tokens") or 0, ctx.get("percent") or 0
        print(f"        第 {turn} 轮后 used={used} pct={pct}%", flush=True)
        if pct >= 100:
            over = ctx
            break

    if over is None:
        record("W-0 制造出 >100% 的窗口期", False, "没能把用量推过 100%")
        print("\n通过 0 / 失败 1")
        return 1

    record("W-0 制造出 >100% 的窗口期（复现用户怀疑的状态）", True,
           f"used={over['used_tokens']} / {over['context_window']} = {over['percent']}%")

    # 2) 就在这个窗口里立刻发下一条 —— 这是用户会做的事
    t0 = time.monotonic()
    ok_send, err, raw = True, "", ""
    try:
        raw, ev = c.stream(sid, "继续：用一句话说明上一段的结论。不要用工具。")
    except Exception as e:  # noqa: BLE001
        ok_send, err = False, f"{type(e).__name__}: {e}"
        ev = []
    elapsed = time.monotonic() - t0
    record("W-1 超窗窗口期仍能发送消息（不报错/不超时）", ok_send,
           err or f"耗时 {elapsed:.1f}s")

    text = "".join(e.get("text", "") for e in ev if e.get("type") in ("text_delta", "message_delta"))
    errors = [e for e in ev if e.get("type") == "error"]
    record("W-2 这一轮有真实回复内容（不是空/不是纯报错）",
           len(text.strip()) > 5 and not errors,
           f"回复 {len(text)} 字" + (f"；error={errors[0].get('error')}" if errors else ""))

    # 3) 之后用量回到软线以下
    time.sleep(3)
    after = c.get(f"/api/sessions/{sid}/context")
    record("W-3 之后用量回到软线以下（硬线兜底或后台压缩生效）",
           (after.get("used_tokens") or 0) < soft,
           f"used={after.get('used_tokens')} < soft={soft}；pct={after.get('percent')}%")

    msgs = c.get(f"/api/sessions/{sid}").get("messages") or []
    record("W-4 历史没被抹空", len(msgs) >= 1, f"{len(msgs)} 条消息")

    failed = [n for n, ok, _ in results if not ok]
    print()
    print(f"通过 {len(results) - len(failed)} / 失败 {len(failed)}")
    for n in failed:
        print(f"  ✗ {n}")
    out = ROOT / "backend" / "tests" / "e2e" / "runs" / f"{RUN_ID}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(
        {"run_id": RUN_ID, "session_id": sid, "window": window, "soft": soft,
         "at_over": over, "after": after,
         "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"现场：{out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
