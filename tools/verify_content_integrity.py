"""核实：正文内容真的丢失，还是我的解析器丢的？

用 httpx（和 Hlaudz 实际使用的一致）而非手搓 socket 解析，
对比两种解析出的正文是否相同。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
HOST = "https://ecloud.10086.cn/api/query/aigateway/v1"
MODEL = "moma/deepseek-latest-flash"


def load_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ZhanLu_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no key")


def run(extra: dict, label: str):
    payload = {
        "model": MODEL,
        "messages": [{"role": "user",
                      "content": "一个笼子里有鸡和兔共 35 只，脚共 94 只。各几只？只给答案。"}],
        "max_tokens": 800,
        "temperature": 1.0,
        "stream": True,
        **extra,
    }
    content, reasoning = [], []
    usage = None
    raw_events = 0
    with httpx.Client(timeout=httpx.Timeout(180.0, connect=20.0)) as c:
        with c.stream("POST", f"{HOST}/chat/completions",
                      json=payload,
                      headers={"Authorization": f"Bearer {load_key()}",
                               "Accept": "text/event-stream"}) as r:
            print(f"\n  [{label}] status={r.status_code}")
            if r.status_code != 200:
                print(f"    错误: {r.read()[:300]}")
                return
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
                raw_events += 1
                if ev.get("usage"):
                    usage = ev["usage"]
                ch = (ev.get("choices") or [{}])[0]
                dl = ch.get("delta") or {}
                # 关键：分别收集 finish_reason，用来判断是否被截断
                if dl.get("content"):
                    content.append(dl["content"])
                rc = dl.get("reasoning_content") or dl.get("reasoning")
                if rc:
                    reasoning.append(rc)
                if ch.get("finish_reason"):
                    print(f"    finish_reason={ch['finish_reason']}")

    text = "".join(content)
    print(f"    SSE 事件 {raw_events} 个")
    print(f"    正文 {len(text)} 字符: {text!r}")
    print(f"    思考 {len(''.join(reasoning))} 字符")
    if usage:
        det = usage.get("completion_tokens_details") or {}
        print(f"    completion={usage.get('completion_tokens')} "
              f"reasoning={det.get('reasoning_tokens')} "
              f"text={det.get('text_tokens')}")
    # 完整性判据：答案必须含 "12"
    has_12 = "12" in text
    print(f"    答案含 '12'（兔的只数）: {has_12}  {'OK' if has_12 else '<< 疑似内容丢失'}")


if __name__ == "__main__":
    print("=" * 70)
    print("用 httpx（与 Hlaudz 一致的客户端）复核正文完整性")
    print("=" * 70)
    for i in range(3):
        run({}, f"默认 #{i+1}")
    for i in range(3):
        run({"enable_thinking": False}, f"关思考 #{i+1}")