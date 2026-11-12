"""实测湛卢网关的分段延迟：DNS / TCP / TLS / 首字节 / 全量。

分清是网络慢、网关慢，还是模型慢。
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

HOST = "ecloud.10086.cn"
PATH = "/api/query/aigateway/v1/chat/completions"
MODEL = "moma/deepseek-latest-flash"


def load_key() -> str:
    env = Path(__file__).resolve().parents[1] / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("ZhanLu_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("未找到 ZhanLu_API_KEY")


def net_probe():
    print("=== 1. DNS ===")
    t0 = time.perf_counter()
    infos = socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)
    dns_ms = (time.perf_counter() - t0) * 1000
    addrs = sorted({i[4][0] for i in infos})
    print(f"  解析 {dns_ms:.1f} ms -> {addrs}")

    print("\n=== 2. TCP + TLS（3 次取样）===")
    for i in range(3):
        t = time.perf_counter()
        sock = socket.create_connection((HOST, 443), timeout=15)
        tcp_ms = (time.perf_counter() - t) * 1000

        ctx = ssl.create_default_context()
        t2 = time.perf_counter()
        ssock = ctx.wrap_socket(sock, server_hostname=HOST)
        tls_ms = (time.perf_counter() - t2) * 1000
        print(f"  #{i+1} TCP {tcp_ms:7.1f} ms   TLS {tls_ms:7.1f} ms   "
              f"合计 {tcp_ms + tls_ms:7.1f} ms")
        ssock.close()


def chat_probe(key: str, prompt: str, stream: bool, max_tokens: int, label: str):
    """发一次真实请求，记录首字节与总时长。"""
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 1.0,
        "stream": stream,
    }).encode()

    ctx = ssl.create_default_context()
    t_start = time.perf_counter()
    sock = socket.create_connection((HOST, 443), timeout=20)
    ssock = ctx.wrap_socket(sock, server_hostname=HOST)
    t_conn = time.perf_counter()

    req = (
        f"POST {PATH} HTTP/1.1\r\n"
        f"Host: {HOST}\r\n"
        f"Authorization: Bearer {key}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Accept: {'text/event-stream' if stream else 'application/json'}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode() + body
    ssock.sendall(req)

    first_byte_t = None
    chunks = []
    while True:
        data = ssock.recv(65536)
        if not data:
            break
        if first_byte_t is None:
            first_byte_t = time.perf_counter()
        chunks.append(data)
    t_end = time.perf_counter()
    ssock.close()

    raw = b"".join(chunks)
    head, _, payload = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n")[0].decode("latin-1", "replace")

    ttfb = ((first_byte_t or t_end) - t_start) * 1000
    total = (t_end - t_start) * 1000
    print(f"\n  [{label}] {status_line}")
    print(f"    建连+握手 {(t_conn - t_start)*1000:.1f} ms   "
          f"首字节 {ttfb:.1f} ms   总耗时 {total:.1f} ms   "
          f"响应 {len(payload)} B")

    if stream:
        texts = []
        for line in payload.decode("utf-8", "replace").splitlines():
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d in ("", "[DONE]"):
                continue
            try:
                ev = json.loads(d)
            except Exception:
                continue
            ch = (ev.get("choices") or [{}])[0]
            piece = (ch.get("delta") or {}).get("content") or ""
            if piece:
                texts.append(piece)
        out = "".join(texts)
        print(f"    流式拼出 {len(out)} 字符: {out[:80]!r}")
    else:
        try:
            obj = json.loads(payload)
            msg = obj["choices"][0]["message"].get("content", "")
            usage = obj.get("usage") or {}
            print(f"    非流式 {len(msg)} 字符: {msg[:80]!r}")
            if usage:
                print(f"    usage: {usage}")
        except Exception as e:
            print(f"    解析失败: {e}; 前 200B: {payload[:200]!r}")
    return total


def main():
    key = load_key()
    net_probe()

    print("\n=== 3. 真实模型请求 ===")
    chat_probe(key, "只回复两个字：好的", stream=False, max_tokens=20,
               label="非流式·极短")
    chat_probe(key, "只回复两个字：好的", stream=True, max_tokens=20,
               label="流式·极短")
    chat_probe(key, "用一句话说明什么是 TCP 三次握手。", stream=True,
               max_tokens=200, label="流式·中等")
    # 模拟 Hlaudz 实际每轮都会带的系统提示词规模
    big = "以下是背景资料，请忽略并只回答最后的问题。\n" + ("背景填充。" * 2000) + "\n问题：回复 OK"
    chat_probe(key, big, stream=True, max_tokens=50, label="流式·约 2 万字符输入")


if __name__ == "__main__":
    main()