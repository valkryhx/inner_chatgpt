"""端到端测 Hlaudz 一轮对话的分段耗时，定位卡在哪一段。

对正在运行的后端发一条真实消息，逐事件打时间戳：
  请求发出 -> 第一个 SSE 事件 -> 第一个 text_delta -> done
"""
from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"


def call(path, data=None, token=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body,
                                 method="POST" if body else "GET")
    if body:
        req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def probe(token: str, sess_id: str, text: str, label: str):
    print(f"\n--- {label} ---")
    body = json.dumps({"text": text, "mode": "send"}).encode()
    conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=300)
    t0 = time.perf_counter()
    conn.request("POST", f"/api/sessions/{sess_id}/messages", body=body,
                 headers={"content-type": "application/json",
                          "authorization": "Bearer " + token})
    resp = conn.getresponse()
    t_headers = time.perf_counter()
    print(f"  HTTP 响应头: {(t_headers-t0)*1000:.0f} ms  status={resp.status}")

    first_event = None
    first_text = None
    done_t = None
    n_text = 0
    n_tool = 0
    n_all = 0
    text_chars = 0
    kinds = {}

    buf = b""
    cur_event = None
    while True:
        chunk = resp.read(1)
        if not chunk:
            break
        buf += chunk
        if not buf.endswith(b"\n"):
            continue
        line = buf.decode("utf-8", "replace").rstrip("\r\n")
        buf = b""
        # SSE 帧形如： event: <type>  /  data: <json>
        # 事件类型在 event 行，不在 json 里——只读 data 会全部读成 "?"。
        if line.startswith("event:"):
            cur_event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        now = time.perf_counter()
        et = cur_event or ev.get("type") or "?"
        kinds[et] = kinds.get(et, 0) + 1
        n_all += 1
        if first_event is None:
            first_event = now
        if et == "text_delta":
            n_text += 1
            text_chars += len(ev.get("text", ""))
            if first_text is None:
                first_text = now
        if et in ("tool_start",):
            n_tool += 1
        if et == "done":
            done_t = now
            break

    conn.close()
    ref = first_event or t_headers
    print(f"  首个 SSE 事件: +{((first_event or t_headers)-t0)*1000:.0f} ms")
    print(f"  首个文本增量: {'+' + str(round(((first_text or ref)-t0)*1000)) + ' ms' if first_text else '无'}")
    print(f"  结束(done):   {'+' + str(round(((done_t or time.perf_counter())-t0)*1000)) + ' ms' if done_t else '无 done'}")
    print(f"  事件 {n_all} 个 / text_delta {n_text} / tool_start {n_tool} / {text_chars} 字符")
    print(f"  事件类型: {dict(sorted(kinds.items(), key=lambda x: -x[1])[:8])}")
    return {
        "headers_ms": (t_headers - t0) * 1000,
        "first_event_ms": ((first_event or t_headers) - t0) * 1000,
        "first_text_ms": ((first_text - t0) * 1000) if first_text else None,
        "done_ms": ((done_t or time.perf_counter()) - t0) * 1000,
        "text_deltas": n_text,
    }


def main():
    import random, string
    sfx = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    email = f"lat_{sfx}@example.invalid"
    pw = "Lat#12345"
    auth = call("/api/auth/register",
                {"email": email, "password": pw, "name": "延迟探针"})
    tok = auth["access_token"]
    print(f"临时账号 {email}")

    s1 = call("/api/sessions", {"title": "新会话", "model_id": ""}, token=tok)
    print(f"新会话 {s1['id']}")

    r1 = probe(tok, s1["id"], "只回复两个字：好的", "第 1 轮（空历史，极短问题）")
    r2 = probe(tok, s1["id"], "再回复两个字：收到", "第 2 轮（带 1 轮历史）")
    r3 = probe(tok, s1["id"], "用一句话解释什么是三次握手", "第 3 轮（带 2 轮历史）")

    print("\n" + "=" * 64)
    print("汇总")
    print(f"{'轮次':<10}{'首事件':>10}{'首文本':>10}{'完成':>10}{'文本增量':>10}")
    for label, r in [("第1轮", r1), ("第2轮", r2), ("第3轮", r3)]:
        ft = f"{r['first_text_ms']:.0f}" if r["first_text_ms"] else "-"
        print(f"{label:<10}{r['first_event_ms']:>9.0f}ms{ft:>9}ms"
              f"{r['done_ms']:>9.0f}ms{r['text_deltas']:>10}")

    print("\n注：首事件 - 请求 = 服务端建 runtime + 调模型的等待；")
    print("    首文本 - 首事件 = 模型吐第一个字的思考时间。")


if __name__ == "__main__":
    main()