"""后台软线压缩「能不能跑完」——真机验证赛后压缩是否真的发生。

## 为什么单独验这个

`overwindow_behavior.py`（runId 1794719482-0981）实测发现：那一轮用量
`33686 / 30000 = 112.29%`，但历史里出现的是 `_source=hard_trim`，而 app.log 里
连续三次 `已抢占上一轮的赛后压缩，本轮硬线优先`。

也就是说：**赛后软线压缩被调度了，但每轮都被下一轮抢占，一次都没跑完。**
我的压测脚本每轮只间隔几秒就发下一条，正好踩在压缩在飞的时间窗里。

这引出一个必须回答的问题：**真实用户（发完会看几秒再发）能不能等到压缩跑完？**

如果答案是「能」，那 >100% 只是我的压测方式造成的，不是产品缺陷。
如果「不能」或者「压缩根本不会生效」，那是真缺陷。

## 怎么验

1. 充到 >= 软线（用小窗口加速）；
2. **停手等待**（不发送），按 0s / 3s / 10s / 30s / 60s 采样 `/context`；
3. 断言：
   - C-1 等待期间用量自己降下来（不需要用户再发消息）；
   - C-2 降幅是「摘要级」而不是「硬裁级」—— 历史里出现 `summary_` 前缀的消息；
   - C-3 用同样的等待再发一条，仍能正常对话。

C-2 是关键：只看 used 下降会被硬线裁剪骗过。必须看到软线摘要的产物。

## 跑法

    python tools/turn_job_verify/deferred_compact_live.py --port 8117 --window 40000
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

    def stream(self, sid: str, text: str, timeout=600) -> list[dict]:
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
        return events


def history_sources(c: C, sid: str) -> list[str]:
    """直接从库里读 `_source` 字段，判断压缩产物是摘要还是硬裁。

    为什么不用 API：会话详情接口返回的是 `messages` 表（展示用），
    而压缩改写的是 `session_history`（模型看到的那份）。两者不是一回事，
    查错了会得出「没有摘要」的错误结论。
    """
    import sqlite3
    db_path = ROOT / "work" / "auto150k" / "app.db"
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = db.execute(
            "select history from session_history where session_id=?", (sid,)).fetchone()
        db.close()
    except Exception as e:  # noqa: BLE001
        return [f"<err {e}>"]
    if not row:
        return []
    msgs = json.loads(row[0])
    return [str(m.get("_source") or "") for m in msgs]


def main(argv: list[str]) -> int:
    port = 8117
    window = 40000
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if "--window" in argv:
        window = int(argv[argv.index("--window") + 1])
    base = f"http://127.0.0.1:{port}"
    soft = int(window * 0.9)

    print("=" * 74)
    print(f"赛后软线压缩能否自己跑完  runId={RUN_ID}  窗口={window} 软线={soft}")
    print("=" * 74, flush=True)

    c = C(base)
    email = f"defer_{RUN_ID.replace('-', '_')}@t.local"
    c.token = c.post("/api/auth/register",
                     {"email": email, "password": "pw12345678",
                      "name": "DeferLive"}).get("access_token") or ""
    from tests.e2e.harness import zhanlu_api_key
    c.post("/api/me/llm-configs", {
        "label": "defer", "wire_api": "openai_chat",
        "base_url": "https://ecloud.10086.cn/api/query/aigateway/v1",
        "model": "moma/deepseek-latest-flash",
        "context_window": window, "max_tokens": 8192,
        "is_default": True, "api_key": zhanlu_api_key(),
    })
    sid = c.post("/api/sessions", {"title": "defer-live"})["id"]

    big = ("写一篇 12000 字的技术长文，主题自选，分 24 个小节，"
           "每节独立标题和完整段落。不要用工具。")

    # 1) 充到 >= 软线
    crossed = False
    for turn in range(1, 15):
        c.stream(sid, big)
        ctx = c.get(f"/api/sessions/{sid}/context")
        used, pct = ctx.get("used_tokens") or 0, ctx.get("percent") or 0
        print(f"        第 {turn} 轮后 used={used} pct={pct}%", flush=True)
        if used >= soft:
            crossed = True
            break

    if not crossed:
        record("C-0 充到软线以上", False, "轮数用完仍未跨软线")
        print("\n通过 0 / 失败 1")
        return 1
    record("C-0 充到软线以上（此时后台压缩应被调度）", True,
           f"used={used} >= soft={soft}")

    at_cross = c.get(f"/api/sessions/{sid}/context")
    # 2) 停手等待 —— 这块是本次验证的核心：用户不再发消息
    marks = [0, 3, 10, 30, 60]
    samples: list[dict] = []
    prev = 0
    for wait in marks:
        if wait > prev:
            time.sleep(wait - prev)
            prev = wait
        ctx = c.get(f"/api/sessions/{sid}/context")
        srcs = history_sources(c, sid)
        # `_source` 的真实值是 `SUMMARY_SOURCE_VAL = "summarization"`（见 app/tokens.py），
        # 不是 "summary"。判据写错会得出「没有摘要」的假缺陷。
        has_summary = any(s == "summarization" for s in srcs)
        has_trim = any(s == "hard_trim" for s in srcs)
        samples.append({"wait_s": wait, "used": ctx.get("used_tokens"),
                        "pct": ctx.get("percent"), "summary": has_summary,
                        "trim": has_trim, "msgs": len(srcs)})
        print(f"        等 {wait:>2}s：used={ctx.get('used_tokens')} "
              f"pct={ctx.get('percent')}% 摘要={has_summary} 硬裁={has_trim} "
              f"({len(srcs)} 条)", flush=True)

    dropped = (at_cross.get("used_tokens") or 0) - (samples[-1]["used"] or 0)
    record("C-1 等待期间用量自己降下来（用户不需要再发消息）", dropped > 0,
           f"{at_cross.get('used_tokens')} → {samples[-1]['used']}（降 {dropped}）")

    summary_seen = any(s["summary"] for s in samples)
    record("C-2 降幅来自软线摘要而不是硬裁（历史里出现 summary 消息）",
           summary_seen,
           "见 summary 消息" if summary_seen else
           f"只见到硬裁：{[s['trim'] for s in samples]}")

    ok_send, err = True, ""
    try:
        c.stream(sid, "用一句话总结刚才那篇长文的核心论点。不要用工具。")
    except Exception as e:  # noqa: BLE001
        ok_send, err = False, f"{type(e).__name__}: {e}"
    record("C-3 等待后仍能正常对话", ok_send, err or "成功")

    failed = [n for n, ok, _ in results if not ok]
    print()
    print(f"通过 {len(results) - len(failed)} / 失败 {len(failed)}")
    for n in failed:
        print(f"  ✗ {n}")

    out = ROOT / "backend" / "tests" / "e2e" / "runs" / f"{RUN_ID}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(
        {"run_id": RUN_ID, "session_id": sid, "window": window, "soft": soft,
         "at_cross": at_cross, "samples": samples,
         "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"现场：{out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
