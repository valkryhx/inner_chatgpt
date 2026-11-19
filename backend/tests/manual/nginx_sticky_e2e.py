"""真机验证 nginx 粘性负载均衡。

## 为什么必须真起 nginx

单测只能验证「应用下发了 cookie」，证明不了：
- nginx 的 `hash $cookie_x consistent` 真的按 cookie 分流
- 同一个 cookie 反复请求真的落同一个实例
- 不同 cookie 真的能落到不同实例

## 怎么知道「落在哪个实例」

用应用打的 `X-HLZ-Instance` 响应头（`<host>:<pid>`）。
一开始想用 nginx 的 `$upstream_addr` 日志，但 Git Bash 会把
容器内路径 `/var/log/...` 转成 Windows 路径（`D:/Git/var/log/...`），
`docker exec cat` 直接失败。响应头更直接，且不依赖日志格式。

## 拓扑

    client -> nginx:8899 -> { app:8801, app:8802 }

用法：
    python tests/manual/nginx_sticky_e2e.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

RESULTS: list[tuple[str, bool, str]] = []
NGINX_CONTAINER = "hlz-nginx-e2e"
APP_A, APP_B, NGINX_PORT = 8801, 8802, 8899


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def start_instance(port: int, tag: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["HLZ_INSTANCE_ID"] = tag
    env["HLZ_SANDBOX_MODE"] = "off"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(BACKEND.resolve())
    db = BACKEND / "work" / f"_nginx_{tag}.db"
    if db.exists():
        db.unlink()
    env["HLZ_DB_URL"] = f"sqlite:///{db.as_posix()}"

    code = ("import uvicorn; "
            f"uvicorn.run('app.main:app', host='127.0.0.1', port={port}, "
            "log_level='warning')")
    log = BACKEND / "work" / f"_nginx_{tag}.log"
    fh = open(log, "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen([sys.executable, "-c", code], env=env,
                         cwd=str(BACKEND), stdout=fh, stderr=subprocess.STDOUT)
    p._hlz_log = str(log)      # type: ignore[attr-defined]
    return p


def http_get(port: int, path: str, cookie: str = "", timeout: float = 8.0):
    """返回 (完整响应文本, 头部字典)。"""
    import socket
    hdrs = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Connection: close"]
    if cookie:
        hdrs.append(f"Cookie: {cookie}")
    req = ("\r\n".join(hdrs) + "\r\n\r\n").encode()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.sendall(req)
        buf = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            buf.append(b)
        s.close()
        text = b"".join(buf).decode("utf-8", "replace")
        head, _, _ = text.partition("\r\n\r\n")
        headers = {}
        for line in head.split("\r\n")[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        return text, headers
    except OSError as exc:
        return f"__ERROR__ {exc}", {}


def wait_up(port: int, timeout: float = 90.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        body, _ = http_get(port, "/api/health", timeout=1.0)
        if "__ERROR__" not in body:
            return True
        time.sleep(0.5)
    return False


def extract_cookie(resp: str) -> str:
    m = re.search(r"set-cookie:\s*(hlz_sticky=[^;\r\n]+)", resp, re.IGNORECASE)
    return m.group(1) if m else ""


def start_nginx() -> bool:
    subprocess.run(["docker", "rm", "-f", NGINX_CONTAINER], capture_output=True)
    conf = BACKEND / "deploy" / "_nginx_e2e.conf"
    conf.write_text(f"""
worker_processes 1;
error_log /dev/stderr warn;
pid /tmp/nginx.pid;
events {{ worker_connections 256; }}
http {{
    access_log /dev/stdout;
    client_body_temp_path /tmp/cbt;
    proxy_temp_path /tmp/pt;
    fastcgi_temp_path /tmp/ft;
    uwsgi_temp_path /tmp/ut;
    scgi_temp_path /tmp/st;

    upstream hlz {{
        # ⚠️ 这里用**两个不同端口**的 server，和 `--scale` 的场景不同。
        # 2026-11-19 实测：`server app:8000` 由 docker DNS 解析成 3 个 A 记录时，
        # `hash ... consistent` 会退化成严格轮询（ketama 环按 server **名字**
        # 建点，三个同名 peer 在环上重合）。本脚本用不同端口，环上名字不同，
        # 所以 `consistent` 能正常工作 —— **这正说明本脚本抓不到那个 bug**。
        # 那个 bug 由 `tests/manual/compose_scale_e2e.py` 覆盖（真 compose --scale）。
        hash $cookie_hlz_sticky consistent;
        server host.docker.internal:{APP_A};
        server host.docker.internal:{APP_B};
        keepalive 16;
    }}
    server {{
        listen 80;
        location / {{
            proxy_pass http://hlz;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host $host;
            proxy_buffering off;
            proxy_cache off;
            chunked_transfer_encoding on;
            proxy_read_timeout 3600s;
        }}
    }}
}}
""", encoding="utf-8", newline="\n")

    r = subprocess.run([
        "docker", "run", "-d", "--name", NGINX_CONTAINER,
        "-p", f"{NGINX_PORT}:80",
        "--add-host", "host.docker.internal:host-gateway",
        "-v", f"{conf.resolve()}:/etc/nginx/nginx.conf:ro",
        "nginx:alpine",
    ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("   nginx 启动失败:", (r.stderr or "")[-600:])
        return False
    return True


def main() -> int:
    pA = pB = None
    try:
        print("=== 起两个后端 ===")
        pA = start_instance(APP_A, "A")
        pB = start_instance(APP_B, "B")
        okA, okB = wait_up(APP_A), wait_up(APP_B)
        check("实例 A 启动", okA)
        check("实例 B 启动", okB)
        if not (okA and okB):
            for p in (pA, pB):
                try:
                    print(Path(p._hlz_log).read_text(encoding="utf-8")[-2000:])
                except Exception:  # noqa: BLE001
                    pass
            return 1

        # 先确认两个实例确实不同
        _, ha = http_get(APP_A, "/api/health")
        _, hb = http_get(APP_B, "/api/health")
        inst_a, inst_b = ha.get("x-hlz-instance", ""), hb.get("x-hlz-instance", "")
        check("两个实例身份不同", bool(inst_a and inst_b and inst_a != inst_b),
              f"A={inst_a} B={inst_b}")

        print("\n=== 起 nginx ===")
        if not start_nginx():
            check("nginx 启动", False)
            return 1
        time.sleep(2)
        body, h = http_get(NGINX_PORT, "/api/health")
        check("nginx 转发可用", "__ERROR__" not in body, h.get("x-hlz-instance", body[:60]))

        # ---- 粘性 ----
        print("\n=== 粘性验证 ===")
        first, _ = http_get(NGINX_PORT, "/api/health")
        cookie = extract_cookie(first)
        check("经 nginx 能拿到粘性 cookie", bool(cookie), cookie or "(空)")

        hits: list[str] = []
        for _ in range(10):
            _, hh = http_get(NGINX_PORT, "/api/health", cookie=cookie)
            i = hh.get("x-hlz-instance", "")
            if i:
                hits.append(i)
        uniq = sorted(set(hits))
        check("同一 cookie 始终落同一实例", len(uniq) == 1,
              f"命中 {uniq}（{len(hits)} 次采样）")

        # ---- 分散 ----
        print("\n=== 分散验证 ===")
        seen: set[str] = set()
        for _ in range(16):
            c = f"hlz_sticky={uuid.uuid4().hex}"
            _, hh = http_get(NGINX_PORT, "/api/health", cookie=c)
            i = hh.get("x-hlz-instance", "")
            if i:
                seen.add(i)
        check("不同 cookie 能分散到两个实例", len(seen) >= 2, f"命中 {sorted(seen)}")

        # ---- 无 cookie ----
        print("\n=== 无 cookie ===")
        _, hh = http_get(NGINX_PORT, "/api/health")
        check("无 cookie 也能正常响应", bool(hh.get("x-hlz-instance")),
              hh.get("x-hlz-instance", ""))

        # ---- SSE 不缓冲 ----
        print("\n=== SSE 兼容性 ===")
        t0 = time.time()
        http_get(NGINX_PORT, "/api/health")
        dt = time.time() - t0
        check("经 nginx 的请求及时返回（未被缓冲卡住）", dt < 3.0, f"{dt:.2f}s")

    finally:
        subprocess.run(["docker", "rm", "-f", NGINX_CONTAINER], capture_output=True)
        for p in (pA, pB):
            if p is not None and p.poll() is None:
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        # 清掉测试库
        for tag in ("A", "B"):
            db = BACKEND / "work" / f"_nginx_{tag}.db"
            try:
                if db.exists():
                    db.unlink()
            except OSError:
                pass

    print("\n=== 汇总 ===")
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"{len(RESULTS)-len(bad)}/{len(RESULTS)} 通过")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())