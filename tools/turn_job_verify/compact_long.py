"""压缩的长文本场景：内容够长时压缩必须真的省 token。

## 为什么与 `compact_deep.py` 分开

`compact_deep.py` 用极短问答（每轮十几字）验「压缩不毁历史、摘要真的进上下文」。
但那里**不能**断言 token 下降 —— 实测压掉约 60 tokens 的短问答，换来的摘要是
304 字 / 175 tokens，反而涨了。这是摘要的固定开销，不是缺陷。

本脚本反过来：每轮塞**长正文**（几千字），让压缩一定划算，然后断言：

| 编号 | 断言 |
|---|---|
| L-1 | `/compact` 返回 compacted=true |
| L-2 | `used_tokens` **严格下降**（长内容场景，这是压缩的唯一价值） |
| L-3 | 压缩比合理：`after/before < 0.8`（省不到 20% 就是白压） |
| L-4 | 压缩后仍能继续对话 |

## 跑法

    python tools/turn_job_verify/compact_long.py --base http://127.0.0.1:PORT
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

    def _req(self, path, payload=None, method="GET", timeout=400):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data,
            headers={"content-type": "application/json",
                     **({"authorization": f"Bearer {self.token}"} if self.token else {})},
            method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode() or "{}"
            return json.loads(body) if body.strip().startswith(("{", "[")) else body

    def post(self, p, payload=None, timeout=400):
        return self._req(p, payload, "POST", timeout)

    def get(self, p, timeout=120):
        return self._req(p, None, "GET", timeout)


def main(argv: list[str]) -> int:
    base = "http://127.0.0.1:8000"
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]

    print("=" * 72)
    print(f"压缩长文本场景  runId={RUN_ID}")
    print("=" * 72, flush=True)

    c = C(base)
    email = f"cmlong_{RUN_ID.replace('-', '_')}@t.local"
    c.token = c.post("/api/auth/register",
                     {"email": email, "password": "pw12345678",
                      "name": "CompactLong"}).get("access_token") or ""
    from tests.e2e.harness import DEFAULT_LLM, zhanlu_api_key
    cfg = dict(DEFAULT_LLM)
    cfg["api_key"] = zhanlu_api_key()
    c.post("/api/me/llm-configs", cfg)
    sid = c.post("/api/sessions", {"title": "compact-long"})["id"]

    def send(text: str) -> str:
        req = urllib.request.Request(
            f"{base}/api/sessions/{sid}/messages",
            data=json.dumps({"text": text}).encode(),
            headers={"content-type": "application/json",
                     "accept": "text/event-stream",
                     "authorization": f"Bearer {c.token}"},
            method="POST")
        buf = b""
        with urllib.request.urlopen(req, timeout=400) as res:
            while True:
                chunk = res.read1(4096)
                if not chunk:
                    break
                buf += chunk
        return buf.decode("utf-8", errors="replace")

    # 五轮长正文：每轮让模型写满一段，制造真实的长历史。
    #
    # ⚠️ 第一版只跑了 3 轮（6 条消息），`/compact` 直接回「消息太少（6 条，≤ 保留
    # 阈值 6），无需压缩」—— 压缩器按**条数**判定，3 轮正好卡在阈值上，
    # 核心路径一行没跑到。必须 > keep(6) 条，这里给 5 轮 = 10 条。
    prompts = [
        "写一段 1200 字的说明文，主题是「分布式事务的两阶段提交」。分 4 个小节。不要用工具。",
        "写一段 1200 字的说明文，主题是「B+ 树索引为什么适合范围查询」。分 4 个小节。不要用工具。",
        "写一段 1200 字的说明文，主题是「幂等键在支付系统里的设计」。分 4 个小节。不要用工具。",
        "写一段 1200 字的说明文，主题是「CAP 定理在真实系统里怎么取舍」。分 4 个小节。不要用工具。",
        "写一段 1200 字的说明文，主题是「消息队列的至少一次与恰好一次」。分 4 个小节。不要用工具。",
    ]
    for i, t in enumerate(prompts, 1):
        send(t)
        print(f"        第 {i} 轮完成", flush=True)

    before = c.get(f"/api/sessions/{sid}/context")
    msgs_before = c.get(f"/api/sessions/{sid}").get("messages") or []
    print(f"        压缩前：{len(msgs_before)} 条消息 / {before['used_tokens']} tokens")

    r = c.post(f"/api/sessions/{sid}/compact", {})
    after = c.get(f"/api/sessions/{sid}/context")
    msgs_after = c.get(f"/api/sessions/{sid}").get("messages") or []
    print(f"        压缩后：{len(msgs_after)} 条消息 / {after['used_tokens']} tokens")

    record("L-1 /compact 返回 compacted=true", r.get("compacted") is True,
           f"message={str(r.get('message'))[:90]!r}")
    ratio = after["used_tokens"] / max(1, before["used_tokens"])
    record("L-2 长内容场景 used_tokens 严格下降",
           after["used_tokens"] < before["used_tokens"],
           f"{before['used_tokens']} → {after['used_tokens']}")
    record("L-3 压缩比 < 0.8（省了至少 20%）", ratio < 0.8, f"ratio={ratio:.2f}")

    ok_send = True
    err = ""
    try:
        send("用一句话总结上面三段内容分别讲什么。")
    except Exception as e:  # noqa: BLE001
        ok_send = False
        err = f"{type(e).__name__}: {e}"
    record("L-4 压缩后仍能继续对话", ok_send, err or "成功")

    failed = [n for n, ok, _ in results if not ok]
    print()
    print(f"通过 {len(results) - len(failed)} / 失败 {len(failed)}")
    for n in failed:
        print(f"  ✗ {n}")
    out = ROOT / "backend" / "tests" / "e2e" / "runs" / f"{RUN_ID}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(
        {"run_id": RUN_ID, "session_id": sid, "before": before, "after": after,
         "compact_result": r, "ratio": ratio,
         "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"现场：{out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
