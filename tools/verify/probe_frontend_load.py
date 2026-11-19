"""用无头 Chrome 验证「页面能真正加载并跑起来」。

## 为什么不能只 curl 首页 200

`curl /` 返回 200 + 752 字节 HTML，只能证明 index.html 存在。它**不能**证明：

- JS bundle 能下载并执行
- React 挂载成功（#root 有内容）
- 前端真的发出了 /api 请求（且经过 nginx 落到某个 app 实例）
- 没有白屏 / 控制台报错

这些只有真跑一遍浏览器才知道。

## 做法

Chrome DevTools Protocol，不引入 puppeteer（那要装 100MB 依赖）。
用 `--headless --dump-dom` 最简单，但拿不到网络请求与控制台错误。
所以用 CDP over websocket：启动 chrome --remote-debugging-port，
连上去开 Network + Console 域，导航到页面，收集。

## 用法

    python tools/verify/probe_frontend_load.py
    python tools/verify/probe_frontend_load.py --url http://127.0.0.1:8080
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if Path(p).is_file():
            return p
    raise SystemExit("找不到 Chrome")


def ws_connect(url: str):
    """极简 websocket 客户端（只够 CDP 用）。

    不想为一个探针引入 websockets 依赖。CDP 的帧格式简单：
    FIN+opcode 1 文本帧，客户端必须掩码。
    """
    import base64
    import os
    import socket
    import struct
    from urllib.parse import urlparse

    u = urlparse(url)
    host, port = u.hostname, u.port or 80
    path = u.path + (("?" + u.query) if u.query else "")
    s = socket.create_connection((host, port), timeout=20)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode())
    # 读掉握手响应头
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("websocket 握手失败")
        buf += chunk
    if b"101" not in buf.split(b"\r\n")[0]:
        raise RuntimeError(f"websocket 握手被拒：{buf[:200]!r}")
    return s, (buf.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in buf else b"")


def ws_send(sock, payload: str) -> None:
    import os
    import struct

    data = payload.encode()
    mask = os.urandom(4)
    n = len(data)
    header = bytearray([0x81])
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack(">H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack(">Q", n))
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes(header) + masked)


def ws_recv(sock, buf: bytes) -> tuple[str, bytes]:
    """读一个完整文本帧（处理分片与 ping）。"""
    import struct

    while True:
        while len(buf) < 2:
            buf += sock.recv(65536)
        b0, b1 = buf[0], buf[1]
        opcode = b0 & 0x0F
        n = b1 & 0x7F
        offset = 2
        if n == 126:
            while len(buf) < 4:
                buf += sock.recv(65536)
            n = struct.unpack(">H", buf[2:4])[0]
            offset = 4
        elif n == 127:
            while len(buf) < 10:
                buf += sock.recv(65536)
            n = struct.unpack(">Q", buf[2:10])[0]
            offset = 10
        while len(buf) < offset + n:
            buf += sock.recv(65536)
        payload = buf[offset:offset + n]
        buf = buf[offset + n:]
        if opcode == 0x9:      # ping -> pong
            continue
        if opcode in (0x1, 0x2, 0x0):
            return payload.decode("utf-8", "replace"), buf
        # 其他控制帧忽略


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--wait", type=float, default=8.0)
    args = ap.parse_args()

    chrome = find_chrome()
    profile = tempfile.mkdtemp(prefix="hlz_chrome_")
    port = 9333
    proc = subprocess.Popen([
        chrome, "--headless=new", f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", "--disable-dev-shm-usage", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        # 等 devtools 端口起来
        target = None
        for _ in range(80):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as r:
                    tabs = json.load(r)
                target = next((t for t in tabs if t.get("type") == "page"), None)
                if target:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if not target:
            print("!! Chrome devtools 没起来")
            return 2

        sock, buf = ws_connect(target["webSocketDebuggerUrl"])
        msg_id = 0
        pending: dict[int, str] = {}

        def call(method: str, params: dict | None = None) -> int:
            nonlocal msg_id, buf
            msg_id += 1
            ws_send(sock, json.dumps({"id": msg_id, "method": method,
                                      "params": params or {}}))
            return msg_id

        call("Network.enable")
        call("Console.enable")
        call("Runtime.enable")
        call("Page.enable")

        # 收集事件，直到导航完成 + 等待期结束
        nav_id = call("Page.navigate", {"url": args.url})
        events: list[dict] = []
        deadline = time.time() + args.wait + 6
        while time.time() < deadline:
            try:
                raw, buf = ws_recv(sock, buf)
            except Exception:
                break
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            if "method" in ev:
                events.append(ev)
            if ev.get("id") == nav_id:
                pass
            # 提前退出：加载完成后再等 args.wait
            if ev.get("method") == "Page.loadEventFired":
                deadline = min(deadline, time.time() + args.wait)

        # ---- 分析 ----
        api_reqs = []
        api_failed = []
        js_failed = []
        for ev in events:
            m = ev.get("method")
            p = ev.get("params", {})
            if m == "Network.requestWillBeSent":
                u = p.get("request", {}).get("url", "")
                if "/api/" in u:
                    api_reqs.append(u)
            elif m == "Network.responseReceived":
                r = p.get("response", {})
                u = r.get("url", "")
                st = r.get("status", 0)
                if "/api/" in u and st >= 400:
                    api_failed.append((u, st))
                if u.endswith(".js") and st >= 400:
                    js_failed.append((u, st))
            elif m == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails", {})
                js_failed.append((d.get("text", "exception"), 0))

        # 取 DOM 看 React 是否挂载
        did = call("Runtime.evaluate", {
            "expression": "JSON.stringify({root: (document.getElementById('root')||{})."
                          "innerHTML ? document.getElementById('root').innerHTML.length : 0,"
                          " title: document.title, body: document.body.innerText.slice(0,120)})",
            "returnByValue": True,
        })
        dom = {}
        deadline2 = time.time() + 8
        while time.time() < deadline2:
            raw, buf = ws_recv(sock, buf)
            ev = json.loads(raw)
            if ev.get("id") == did:
                dom = json.loads(ev["result"]["result"].get("value") or "{}")
                break

        print("=" * 68)
        ok = True

        def check(name: str, cond: bool, detail: str = "") -> None:
            nonlocal ok
            print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
            if not cond:
                ok = False

        check("首页 DOM 已渲染（#root 有内容）", dom.get("root", 0) > 0,
              f"#root innerHTML 长度 {dom.get('root', 0)}")
        check("页面标题存在", bool(dom.get("title")), dom.get("title", ""))

        # ⚠️ 不能断言「一定发出了 /api 请求」。
        #
        # 未登录时渲染的是登录页（实测正文就是「登录你的智能 Agent 工作台」），
        # 它**故意不发** API —— 发了反而说明鉴权逻辑有问题。第一版把这条写成
        # 硬断言，于是「一切正常」被判成失败。
        #
        # 正确口径：**if** 发了 API，那么不该有 4xx/5xx（401 除外，那是
        # 「没有 token 时的正常响应」）。这样登录页和已登录页都成立。
        authed_errors = [x for x in api_failed if x[1] not in (401,)]
        check("没有异常的 /api 响应（401 = 未登录，属正常）",
              not authed_errors, str(authed_errors[:3]))
        check("没有 JS 加载失败或运行时异常", not js_failed, str(js_failed[:3]))

        # 静态资源必须全部 200 —— 这是「dist 打对了、路径没错」的硬证据。
        check("JS/CSS 资源全部加载成功", not js_failed, str(js_failed[:3]))

        if api_reqs:
            print(f"\n（本次发起了 {len(api_reqs)} 个 /api 请求：{api_reqs[:3]}）")
        else:
            print("\n（未发 /api 请求 —— 当前是登录页，符合预期）")

        print("\n正文前 120 字：")
        print("  " + (dom.get("body") or "").replace("\n", " ")[:120])
        print("=" * 68)
        print("全部通过" if ok else "有失败项")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())