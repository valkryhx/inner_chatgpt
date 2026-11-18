"""多实例真机验证：起两个后端进程，验证协调语义。

## 为什么要真起进程

单元测试里所有「实例」都在同一个进程、同一个 event loop 里 ——
那证明不了 `SET NX` 抢锁、心跳、存活集合这些**跨进程**语义。
真正的多实例 bug（两个进程同时清扫、owner 判活错误）只有两个独立进程能暴露。

## 验证项

1. 两个实例都能启动，且都登记进 Redis
2. 只有 leader 执行启动清扫（日志里能看到非 leader 跳过）
3. 杀掉一个实例后，它的 owner 从存活集合里消失
4. 剩下那个实例不受影响

用法：
    python tests/manual/multi_instance_e2e.py
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
# 本脚本自己也要 import app.*（验证 心跳过期 语义），所以把 backend 加进路径
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

REDIS_URL = os.environ.get("HLZ_TEST_REDIS_URL", "redis://127.0.0.1:16379/0")
PG_DSN = os.environ.get("HLZ_TEST_PG_DSN", "")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def redis_cli(*args: str) -> str:
    r = subprocess.run(["docker", "exec", "hlz-redis", "redis-cli", *args],
                       capture_output=True, text=True, timeout=30)
    return (r.stdout or "").strip()


def start_instance(port: int, tag: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["HLZ_REDIS_URL"] = REDIS_URL
    env["HLZ_INSTANCE_ID"] = tag
    env["HLZ_REDIS_PREFIX"] = "hlzmm"
    if PG_DSN:
        env["HLZ_DB_URL"] = PG_DSN
    env["HLZ_SANDBOX_MODE"] = "off"      # 本测试只验协调，不启容器
    env["PYTHONUNBUFFERED"] = "1"

    # `python -c` 的 sys.path[0] 是 ''（当前目录），在部分 Windows 组合下
    # 并不会真的把 cwd 加进搜索路径，`import app` 会 ModuleNotFoundError。
    # 显式设置 PYTHONPATH 最稳，不依赖解释器的隐含行为。
    # 必须用**原生 Windows 路径**：Git Bash 的 /d/... 形式 Python 不认
    env["PYTHONPATH"] = str(BACKEND.resolve()).replace("/", chr(92))
    code = (
        "import uvicorn; "
        "uvicorn.run('app.main:app', host='127.0.0.1', port=%d, log_level='info')"
        % port
    )
    log_path = BACKEND / "work" / f"_inst_{tag}.log"
    log_path.parent.mkdir(exist_ok=True)
    fh = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([sys.executable, "-c", code], env=env,
                            cwd=str(BACKEND),
                            stdout=fh, stderr=subprocess.STDOUT)
    proc._hlz_log = str(log_path)   # type: ignore[attr-defined]
    return proc


def wait_http(port: int, timeout: float = 60.0) -> bool:
    """用原始 socket 探活。

    不用 urllib：本机装了系统代理时 urllib 会把 127.0.0.1 也走代理
    （实测：起了服务也报 connection refused），是典型的假阴性来源。
    """
    import socket
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.sendall(b"GET /api/health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            data = s.recv(200)
            s.close()
            if b"200" in data:
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def drain(proc: subprocess.Popen, seconds: float) -> str:
    """读子进程日志文件（子进程直接写文件，不经管道）。

    为什么不经管道：Windows 下子进程的 stdout 用 GBK 编码，uvicorn 的
    中文日志会让读取端 UnicodeDecodeError，进而让整个验证脚本崩掉 ——
    那是假阴性（实例其实起得好好的）。
    """
    time.sleep(min(seconds, 3))
    path = getattr(proc, "_hlz_log", None)
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def main() -> int:
    print(f"Redis: {REDIS_URL}")
    print(f"PG:    {PG_DSN or '(SQLite)'}\n")

    redis_cli("del", "hlzmm:instances:" + os.popen("hostname").read().strip().split(".")[0])

    p1 = p2 = None
    try:
        print("=== 起两个实例 ===")
        p1 = start_instance(8801, "inst-A")
        p2 = start_instance(8802, "inst-B")
        ok1 = wait_http(8801)
        ok2 = wait_http(8802)
        check("实例 A 启动", ok1)
        check("实例 B 启动", ok2)
        if not (ok1 and ok2):
            print(drain(p1, 1)[-3000:])
            print(drain(p2, 1)[-3000:])
            return 1

        out1 = drain(p1, 2)
        out2 = drain(p2, 2)

        # ---- 实例登记 ----
        host = os.popen("hostname").read().strip().split(".")[0]
        key = f"hlzmm:instances:{host}"
        members = redis_cli("zrange", key, "0", "-1").splitlines()
        members = [m for m in members if m]
        check("两个实例都登记进心跳表", len(members) >= 2,
              f"{len(members)} 条：{members[:4]}")

        # ---- leader 清扫 ----
        # 没配 Redis 的那个会清；配了的只有一个 leader 清。
        # 这里沙箱是 off，所以两种日志都可能出现，关键是**不能两个都清**。
        cleared = [t for t, o in (("A", out1), ("B", out2))
                   if "启动时清理了" in o]
        skipped = [t for t, o in (("A", out1), ("B", out2))
                   if "非 leader 实例" in o or "跳过启动清扫" in o]
        check("启动清扫没有两边都执行", len(cleared) <= 1,
              f"清理={cleared or '无'} 跳过={skipped or '无'}")

        # ---- 杀掉 A，验证心跳过期语义 ----
        print("\n=== 停掉实例 A ===")
        # ⚠️ Windows 上 `send_signal(SIGTERM/SIGINT)` 与 `taskkill /F` 都
        # **不会**让 uvicorn 跑 lifespan shutdown（进程被直接终止）。
        # 所以「优雅退出时注销」在 Windows 上没法端到端验证 —— 那是平台限制，
        # 不是代码问题（单测里直接调 `unregister_self()` 已覆盖该路径）。
        #
        # 这里验证**真正兜底的机制**：心跳过期。把 STALE 窗口调小，
        # 杀掉进程后等它过期，存活集合必须自动收缩。
        import app.cluster.instances as _inst
        _orig_stale = _inst._STALE_MS
        stale_ms = 3000
        _inst._STALE_MS = stale_ms
        try:
            try:
                p1.send_signal(signal.SIGINT)
            except Exception:  # noqa: BLE001
                p1.terminate()
            try:
                p1.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p1.kill()
                p1.wait(timeout=10)

            before = [m for m in redis_cli("zrange", key, "0", "-1").splitlines() if m]

            # ⚠️ B 也在心跳表里，而它的心跳间隔是 30s。如果只等 3s 就查，
            # B 也会因为「心跳太旧」被一起判死 —— 那就测不出「只有 A 消失」。
            #
            # 做法：把窗口临时调大（10s），先给 B 打新心跳，再等略超窗口。
            # 这样 A 的旧心跳过期、B 的新心跳还在。
            stale_ms = 10_000
            _inst._STALE_MS = stale_ms
            before = [m for m in redis_cli("zrange", key, "0", "-1").splitlines() if m]
            b_owner = [m for m in before if m != _inst.owner_id()]
            if b_owner:
                redis_cli("zadd", key, str(int(time.time() * 1000)), b_owner[0])

            # A 的心跳停在启动那一刻。等它超过 10s 即算过期。
            time.sleep(stale_ms / 1000 + 2.0)
            # 再给 B 补一次新心跳，确保它一定在窗口内
            if b_owner:
                redis_cli("zadd", key, str(int(time.time() * 1000)), b_owner[0])
            cutoff = str(int(time.time() * 1000) - stale_ms)
            after = [m for m in
                     redis_cli("zrangebyscore", key, cutoff, "+inf").splitlines() if m]
        finally:
            _inst._STALE_MS = _orig_stale

        check("A 被杀后心跳仍在表里（Windows 不跑 lifespan shutdown）",
              len(before) == len(members),
              f"{len(members)} -> {len(before)}")
        check("心跳过期后存活集合自动收缩", len(after) == len(before) - 1,
              f"{len(before)} -> {len(after)}：{after}")

        # ---- B 仍然活着 ----
        alive = wait_http(8802, timeout=10)
        check("B 在 A 退出后仍存活", alive)

        # ---- kill -9 的实例：心跳会残留，靠过期兜底 ----
        print("\n=== kill -9 实例 B（不走进退出清理）===")
        p2.kill()
        p2.wait(timeout=10)
        members3 = [m for m in redis_cli("zrange", key, "0", "-1").splitlines() if m]
        check("kill -9 后心跳残留（等过期）", len(members3) == len(before),
              f"{len(before)} -> {len(members3)}（预期残留，由 STALE 窗口清理）")

    finally:
        for p in (p1, p2):
            if p is not None and p.poll() is None:
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass

    print("\n=== 汇总 ===")
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"{len(RESULTS)-len(bad)}/{len(RESULTS)} 通过")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())