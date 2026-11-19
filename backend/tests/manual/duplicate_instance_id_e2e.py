"""真机验证：所有副本共用同一 hostname 时，owner 会不会撞 / 有没有被检测到。

## 为什么需要这个脚本

真实场景：容器里 PID 恒为 1，编排器若让所有副本 hostname 相同
（hostNetwork、StatefulSet 未设 hostname、某些 PaaS），
所有副本的 owner 就都变成 ``web-0:1``。

**单元测试证明不了这件事**，因为单元测试里所有「实例」在同一进程、
同一 hostname 下被 monkeypatch 出来。必须真起两个进程，
且让它们**真的**看到同一个 hostname。

## 三阶段

1. **撞车复现**：两个进程都设 `HLZ_INSTANCE_ID=dup` -> owner 必须相同
   （前提：它们的 hostname 也相同；本机两进程天然同 hostname）
2. **冲突检测**：两个进程都登记 -> 第二个必须打 WARNING（这是本次修的）
3. **修好之后**：两个进程用不同 `HLZ_INSTANCE_ID` -> owner 不同，
   存活集合有 2 个成员，且没有任何冲突告警

## 环境

需要 `hlz-redis` 容器（端口 16379）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
REDIS_URL = os.environ.get("HLZ_TEST_REDIS_URL", "redis://127.0.0.1:16379/0")
BASE_PORT = 8821

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def redis_cli(*args: str) -> str:
    r = subprocess.run(["docker", "exec", "hlz-redis", "redis-cli", *args],
                       capture_output=True, text=True, timeout=30)
    return (r.stdout or "").strip()


def start(port: int, tag: str, instance_id: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["HLZ_REDIS_URL"] = REDIS_URL
    env["HLZ_REDIS_PREFIX"] = PREFIX
    env["HLZ_SANDBOX_MODE"] = "off"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(BACKEND.resolve()).replace("/", chr(92))
    db = BACKEND / "work" / f"_dup_{tag}.db"
    for suf in ("", "-wal", "-shm"):
        p = Path(str(db) + suf)
        if p.exists():
            p.unlink()
    env["HLZ_DB_URL"] = f"sqlite:///{db.as_posix()}"
    # 每个实例独立的 app.log：stdout 重定向到文件时是**块缓冲**，
    # 日志行可能还没落盘 E2E 就去读了（实测踩过：告警其实打了，读到的是空）。
    # RotatingFileHandler 每条 emit 后 flush，是可靠的判据来源。
    app_log = BACKEND / "work" / f"_dup_{tag}.app.log"
    if app_log.exists():
        app_log.unlink()
    env["HLZ_APP_LOG"] = str(app_log.resolve())
    env["HLZ_APP_LOG_ON"] = "1"
    if instance_id:
        env["HLZ_INSTANCE_ID"] = instance_id
    else:
        env.pop("HLZ_INSTANCE_ID", None)

    code = (f"import uvicorn; uvicorn.run('app.main:app', host='127.0.0.1', "
            f"port={port}, log_level='info')")
    log = BACKEND / "work" / f"_dup_{tag}.log"
    log.parent.mkdir(exist_ok=True)
    fh = open(log, "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen([sys.executable, "-c", code], env=env, cwd=str(BACKEND),
                         stdout=fh, stderr=subprocess.STDOUT)
    p._hlz_log = str(log)          # type: ignore[attr-defined]
    p._hlz_app_log = str(app_log)  # type: ignore[attr-defined]
    p._hlz_port = port             # type: ignore[attr-defined]
    p._hlz_tag = tag               # type: ignore[attr-defined]
    return p


def wait_http(port: int, timeout: float = 90.0) -> bool:
    import socket
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.sendall(b"GET /api/health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            d = s.recv(400)
            s.close()
            if b"200" in d:
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def owner_header(port: int) -> str:
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(b"GET /api/health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        buf = b""
        while True:
            b = s.recv(4096)
            if not b:
                break
            buf += b
        s.close()
        for line in buf.decode("utf-8", "replace").split("\r\n"):
            if line.lower().startswith("x-hlz-instance:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def log_of(p: subprocess.Popen) -> str:
    """读实例日志（优先 app.log：每条 emit 后 flush，不受 stdout 块缓冲影响）。"""
    parts = []
    for attr in ("_hlz_app_log", "_hlz_log"):
        path = getattr(p, attr, None)
        if not path:
            continue
        try:
            parts.append(Path(path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(parts)


def stop(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        p.send_signal(15)
    except Exception:  # noqa: BLE001
        p.terminate()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()


def main() -> int:
    global PREFIX
    PREFIX = "hlzdup" + str(int(time.time()))
    host = os.popen("hostname").read().strip().split(".")[0]
    key = f"{PREFIX}:instances:{host}"
    redis_cli("del", key)
    for suffix in ("", ":claim:"):
        pass

    print(f"Redis: {REDIS_URL}")
    print(f"prefix: {PREFIX}\n")

    procs: list[subprocess.Popen] = []
    try:
        # ---------------- 阶段 1：两个副本共用同一实例标识 ----------------
        print("=== 阶段 1：两个副本都用 HLZ_INSTANCE_ID=dup（模拟同 hostname）===")
        for i in (0, 1):
            p = start(BASE_PORT + i, f"dup{i}", "dup")
            procs.append(p)
            if not wait_http(BASE_PORT + i):
                print(log_of(p)[-3000:])
                return 1
        o = [owner_header(p._hlz_port) for p in procs]
        print(f"     owner: {o}")
        check("两个副本 owner 撞在一起（复现遗留隐患）",
              o[0] == o[1] and bool(o[0]), f"{o}")

        members = [m for m in redis_cli("zrange", key, "0", "-1").splitlines() if m]
        check("存活集合被压成 1 个成员（判活失效的直接证据）",
              len(members) == 1, f"{members}")

        # 冲突告警必须在某个实例的日志里出现
        logs = [log_of(p) for p in procs]
        hit = [p._hlz_tag for p, t in zip(procs, logs) if "实例标识冲突" in t]
        check("冲突被检测到并告警", bool(hit), f"告警来自 {hit or '无'}")
        if hit:
            line = [l for l in logs[0].splitlines() + logs[1].splitlines()
                    if "实例标识冲突" in l]
            print(f"     告警原文: {line[0][:160] if line else ''}")

        for p in procs:
            stop(p)
        procs.clear()
        redis_cli("del", key)
        # 清掉认领键（模拟「运维改正配置后重启」）
        for k in redis_cli("keys", f"{PREFIX}:instances:*:claim:*").splitlines():
            if k:
                redis_cli("del", k)

        # ---------------- 阶段 2：正确注入唯一标识 ----------------
        print("\n=== 阶段 2：两个副本各用唯一 HLZ_INSTANCE_ID ===")
        for i, tag in enumerate(("pod-a", "pod-b")):
            p = start(BASE_PORT + i, f"ok{i}", tag)
            procs.append(p)
            if not wait_http(BASE_PORT + i):
                print(log_of(p)[-3000:])
                return 1
        o2 = [owner_header(p._hlz_port) for p in procs]
        print(f"     owner: {o2}")
        check("注入唯一标识后 owner 不再撞", o2[0] != o2[1] and all(o2), f"{o2}")
        check("owner 体现注入值",
              "pod-a" in o2[0] and "pod-b" in o2[1], f"{o2}")

        members2 = [m for m in redis_cli("zrange", key, "0", "-1").splitlines() if m]
        check("存活集合有 2 个成员（判活恢复）", len(members2) == 2, f"{members2}")

        logs2 = [log_of(p) for p in procs]
        check("没有冲突告警", not any("实例标识冲突" in t for t in logs2))

    finally:
        for p in procs:
            stop(p)
        for f in (BACKEND / "work").glob("_dup_*.db*"):
            try:
                f.unlink()
            except OSError:
                pass
        for k in redis_cli("keys", f"{PREFIX}:*").splitlines():
            if k:
                redis_cli("del", k)

    print("\n=== 汇总 ===")
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"{len(RESULTS)-len(bad)}/{len(RESULTS)} 通过")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())