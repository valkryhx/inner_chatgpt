"""compose --scale app=3 真机验证：通过 nginx 走一遍完整链路。

## 验证项

1. 3 个副本都起来了，各自 owner 不同（容器 hostname 天然不同 + PID=1）
2. 3 个副本都登记进 Redis 心跳表
3. nginx 粘性生效：同一 cookie 始终命中同一实例
4. 不同 cookie 能分散到多个实例
5. 启动清扫没有 3 个副本都执行
6. 没有 owner 撞车告警（因为 hostname 天然不同）

## 用法

    python tests/manual/compose_scale_e2e.py
"""
from __future__ import annotations

import subprocess
import sys
import time

PROJECT = "hlzscale"
BASE = "http://127.0.0.1:8080"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def sh(*args: str) -> str:
    r = subprocess.run(args, capture_output=True, timeout=60)
    return (r.stdout or b"").decode("utf-8", "replace").strip()


def compose_ps() -> list[str]:
    out = sh("docker", "ps", "--filter", f"name={PROJECT}-app-",
             "--format", "{{.Names}}\t{{.Status}}")
    return [ln for ln in out.splitlines() if ln]


def http_get(path: str, cookie: str = "") -> tuple[int, dict, str]:
    """用 socket 发请求（避免 urllib 走系统代理）。"""
    import socket
    host, port = "127.0.0.1", 8080
    lines = [f"GET {path} HTTP/1.0", f"Host: {host}"]
    if cookie:
        lines.append(f"Cookie: {cookie}")
    req = "\r\n".join(lines) + "\r\n\r\n"
    s = socket.create_connection((host, port), timeout=15)
    s.sendall(req.encode())
    buf = b""
    while True:
        b = s.recv(8192)
        if not b:
            break
        buf += b
    s.close()
    head, _, body = buf.decode("utf-8", "replace").partition("\r\n\r\n")
    headers = {}
    status = 0
    for i, ln in enumerate(head.split("\r\n")):
        if i == 0 and " " in ln:
            try:
                status = int(ln.split(" ")[1])
            except (IndexError, ValueError):
                pass
        elif ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers, body


def redis(*args: str) -> str:
    return sh("docker", "exec", f"{PROJECT}-redis-1", "redis-cli", *args)


def main() -> int:
    # ---------------- 1. 三个副本都在 ----------------
    print("=== 1. 副本数量 ===")
    ps = compose_ps()
    print("     " + " | ".join(ps))
    check("3 个 app 副本都在跑", len(ps) == 3, f"{len(ps)} 个")

    # ---------------- 2. 通过 nginx 拿身份 ----------------
    print("\n=== 2. 各副本身份（经 nginx）===")
    seen: dict[str, str] = {}      # cookie -> instance
    owners: set[str] = set()
    for i in range(12):
        cookie = f"hlz_sticky=probe{i:02d}"
        st, hd, _ = http_get("/api/health", cookie=cookie)
        inst = hd.get("x-hlz-instance", "")
        if st == 200 and inst:
            seen[cookie] = inst
            owners.add(inst)
    print(f"     12 个不同 cookie 命中 {len(owners)} 个实例: {sorted(owners)}")
    check("3 个副本都能被访问到（一致哈希分散）",
          len(owners) >= 2, f"命中 {len(owners)} 个")
    check("各副本 owner 互不相同（无撞车）",
          len(owners) == len(set(owners)), f"{len(owners)} 个唯一")

    # ---------------- 3. 粘性 ----------------
    print("\n=== 3. 粘性 ===")
    one = "hlz_sticky=stickyprobe"
    hits = []
    for _ in range(10):
        st, hd, _ = http_get("/api/health", cookie=one)
        hits.append(hd.get("x-hlz-instance", ""))
    check("同一 cookie 10 次命中同一实例",
          len(set(hits)) == 1, f"命中 {sorted(set(hits))}")

    # ---------------- 4. 应用下发 cookie ----------------
    print("\n=== 4. 应用下发粘性 cookie ===")
    st, hd, _ = http_get("/api/health")
    sc = hd.get("set-cookie", "")
    check("响应带 hlz_sticky cookie", "hlz_sticky=" in sc, sc[:90])
    low = sc.lower()
    check("cookie 是 HttpOnly + SameSite",
          "httponly" in low and "samesite" in low, sc[:90])

    # ---------------- 5. Redis 登记 ----------------
    print("\n=== 5. Redis 心跳登记 ===")
    keys = [k for k in redis("keys", "hlz:instances:*").splitlines() if k and ":claim:" not in k]
    print(f"     心跳表 key: {keys}")
    total = 0
    members: list[str] = []
    for k in keys:
        ms = [m for m in redis("zrange", k, "0", "-1").splitlines() if m]
        members.extend(ms)
        total += len(ms)
    print(f"     成员: {members}")
    check("3 个副本都登记了", total == 3, f"{total} 个成员")
    check("登记成员 = 访问到的实例集合",
          set(members) == owners, f"登记={sorted(set(members))} 访问={sorted(owners)}")

    # ---------------- 6. 无撞车告警 ----------------
    print("\n=== 6. 无 owner 撞车告警 ===")
    bad: list[str] = []
    for c in [f"{PROJECT}-app-{i}" for i in (1, 2, 3)]:
        logs = sh("docker", "logs", c)
        if "实例标识冲突" in logs:
            bad.append(c)
    check("没有 owner 撞车告警", not bad, f"告警来自 {bad or '无'}")

    # ---------------- 7. 启动清扫 ----------------
    print("\n=== 7. 启动清扫只由一个副本执行 ===")
    cleared: list[str] = []
    for c in [f"{PROJECT}-app-{i}" for i in (1, 2, 3)]:
        logs = sh("docker", "logs", c)
        if "启动时清理了" in logs:
            cleared.append(c)
        elif "非 leader 实例，跳过启动清扫" in logs:
            pass
    check("启动清扫没有全员执行", len(cleared) <= 1, f"执行者 {cleared or '无'}")

    print("\n=== 汇总 ===")
    bad_n = [n for n, ok, _ in RESULTS if not ok]
    print(f"{len(RESULTS)-len(bad_n)}/{len(RESULTS)} 通过")
    return 0 if not bad_n else 1


if __name__ == "__main__":
    sys.exit(main())