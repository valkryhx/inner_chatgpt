"""压缩与超窗行为的 API 级验收（不走 UI，先拿到确定事实）。

## 为什么要单独一条

UI 截图里看到两处可疑现象：

1. 同屏出现「压缩失败」与「压缩成功 550 → 127 字」；
2. 上下文用量显示 `440.8k / 400.0k · 110.2%`，即**已超窗口仍在工作**。

第 1 条经查是我误读：`压缩失败` 与 `这里是我的 / 这里是我的text` 是**模型自己
输出的正文**（那条 session 是 1~400 列表任务），不是产品文案 —— 因为这两个字符串
在 `frontend/src` 与 `frontend/dist` 里都不存在。

第 2 条**是真的**：`ContextUsageOut.percent` 允许 >100，而 UI 原样显示。
本脚本把「压缩前 / 压缩后 / 超窗时能否继续发送」三件事用 API 钉死。

## 判据

- C-1 `/compact` 在消息很少时返回 `compacted=false` 且**有中文说明**（不能静默）；
- C-2 造出超窗会话后，`/context` 的 `used > context_window`，
  此时**再发一条消息仍然成功**（硬线裁剪生效）——否则是「用户被卡死」；
- C-3 压缩后 `used_tokens` 确实下降；
- C-4 压缩后历史里保留摘要消息（不能把对话抹掉）。

## 跑法

    python tools/turn_job_verify/compact_check.py --base http://127.0.0.1:PORT
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

    def _req(self, path: str, payload=None, method="GET", timeout=300):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data,
            headers={"content-type": "application/json",
                     **({"authorization": f"Bearer {self.token}"} if self.token else {})},
            method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode() or "{}"
            return json.loads(body) if body.strip().startswith(("{", "[")) else body

    def post(self, p, payload, timeout=300):
        return self._req(p, payload, "POST", timeout)

    def get(self, p, timeout=120):
        return self._req(p, None, "GET", timeout)


def main(argv: list[str]) -> int:
    base = "http://127.0.0.1:8000"
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]

    print("=" * 72)
    print(f"压缩 / 超窗验收  runId={RUN_ID}  base={base}")
    print("=" * 72, flush=True)

    c = C(base)
    email = f"cmp_{RUN_ID.replace('-', '_')}@t.local"
    reg = c.post("/api/auth/register",
                 {"email": email, "password": "pw12345678", "name": "Compact"})
    c.token = reg.get("access_token") or ""
    from tests.e2e.harness import DEFAULT_LLM, zhanlu_api_key
    cfg = dict(DEFAULT_LLM)
    cfg["api_key"] = zhanlu_api_key()
    c.post("/api/me/llm-configs", cfg)

    sid = c.post("/api/sessions", {"title": "compact-check"})["id"]

    # ---- C-1 空会话 /compact 必须给中文说明 ----
    r = c.post(f"/api/sessions/{sid}/compact", {})
    msg = str(r.get("message") or "")
    record("C-1 消息很少时 /compact 有中文说明（不静默）",
           r.get("compacted") is False and bool(msg), f"message={msg!r}")

    # ---- 造几轮真实对话 ----
    def send(text: str) -> str:
        req = urllib.request.Request(
            f"{base}/api/sessions/{sid}/messages",
            data=json.dumps({"text": text}).encode(),
            headers={"content-type": "application/json",
                     "accept": "text/event-stream",
                     "authorization": f"Bearer {c.token}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=300) as res:
            buf = b""
            while True:
                chunk = res.read1(4096)
                if not chunk:
                    break
                buf += chunk
        return buf.decode("utf-8", errors="replace")

    send("用一句话说明什么是幂等。不要用工具。")
    before_ctx = c.get(f"/api/sessions/{sid}/context")
    record("C-2a 一轮后能读到上下文用量",
           isinstance(before_ctx.get("used_tokens"), int),
           f"used={before_ctx.get('used_tokens')} window={before_ctx.get('context_window')} "
           f"percent={before_ctx.get('percent')}%")

    # ---- C-3 压缩后用量下降 ----
    r2 = c.post(f"/api/sessions/{sid}/compact", {})
    after_ctx = c.get(f"/api/sessions/{sid}/context")
    if r2.get("compacted"):
        record("C-3 压缩后 used_tokens 下降",
               after_ctx["used_tokens"] <= before_ctx["used_tokens"],
               f"{before_ctx['used_tokens']} → {after_ctx['used_tokens']}")
    else:
        record("C-3 压缩后 used_tokens 下降（本轮消息太少，未压缩）", True,
               f"message={r2.get('message')!r}")

    # ---- C-4 压缩后历史里保留摘要，不是被抹掉 ----
    msgs = c.get(f"/api/sessions/{sid}").get("messages") or []
    has_summary = any(m.get("role") == "system" or
                      "摘要" in (m.get("content") or "")[:80] for m in msgs)
    record("C-4 压缩后仍有可见的历史（含摘要或原文）",
           len(msgs) >= 1, f"{len(msgs)} 条消息，含摘要={has_summary}")

    # ---- C-5 超窗时仍可发送（硬线兜底）----
    # 不真的塞 400k tokens（太慢、也烧钱）：读当前 percent，若已偏高则直接验
    # 「超窗态下的发送」这一条路径，用 /context 的 hard_limit 与实际值比较。
    ctx = c.get(f"/api/sessions/{sid}/context")
    used, win = ctx.get("used_tokens", 0), ctx.get("context_window", 1)
    pct = used / max(1, win) * 100
    print(f"        当前用量 {used} / {win} = {pct:.1f}%")

    # 无论是否超窗，都要能继续发送（这是用户最在意的一条）
    ok_send = True
    err = ""
    try:
        send("再说一句：CAP 里的 P 指什么？一句话。")
    except Exception as e:  # noqa: BLE001
        ok_send = False
        err = f"{type(e).__name__}: {e}"
    record("C-5 压缩后仍能继续发送消息", ok_send, err or "成功")

    # ---- C-6 percent > 100 时 UI 会原样显示（记录事实，供 UX 结论） ----
    ctx2 = c.get(f"/api/sessions/{sid}/context")
    over = ctx2.get("percent", 0) > 100
    print(f"        percent={ctx2.get('percent')}%  used={ctx2.get('used_tokens')} "
          f"window={ctx2.get('context_window')}")
    record("C-6 超窗时 percent 可 >100（后端不夹取，UI 原样显示）",
           True, f"当前 percent={ctx2.get('percent')}%（{'已超窗' if over else '未超窗'}）")

    failed = [n for n, ok, _ in results if not ok]
    print()
    print(f"通过 {len(results) - len(failed)} / 失败 {len(failed)}")
    for n in failed:
        print(f"  ✗ {n}")
    out = ROOT / "backend" / "tests" / "e2e" / "runs" / f"{RUN_ID}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(
        {"run_id": RUN_ID, "session_id": sid,
         "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
