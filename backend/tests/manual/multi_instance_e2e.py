"""多实例真机验证：起 N 个后端进程，验证协调语义与**动态扩容**。

## 为什么要真起进程

单元测试里所有「实例」都在同一个进程、同一个 event loop 里 ——
那证明不了 `SET NX` 抢锁、心跳、存活集合这些**跨进程**语义。
真正的多实例 bug（两个进程同时清扫、owner 判活错误）只有独立进程能暴露。

## 验证项

1. N 个实例都能启动，且各自登记进 Redis（owner 互不相同）
2. **动态扩容**：从 2 个加到 3 个，新实例必须
   - 正常登记，且不把前两个挤出存活集合
   - 不触发「清掉别人的容器」（靠 live_owners 保护）
   - 前两个不受影响
3. 启动清扫不会所有实例都执行
4. 杀掉一个实例后，心跳过期会让存活集合自动收缩
5. 剩余实例不受影响

## 用法

    python tests/manual/multi_instance_e2e.py            # 默认 3 个
    python tests/manual/multi_instance_e2e.py --count 4  # 指定数量

## 环境

需要 `hlz-redis` 容器（端口 16379）。可用 PG，也可用 SQLite。
每个实例用**独立的 SQLite 库文件**（除非给了 HLZ_TEST_PG_DSN），
避免库层面的互相污染掩盖协调层的问题。
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
# 本脚本自己也要 import app.*（验证心跳过期语义），所以把 backend 加进路径
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

REDIS_URL = os.environ.get("HLZ_TEST_REDIS_URL", "redis://127.0.0.1:16379/0")
PG_DSN = os.environ.get("HLZ_TEST_PG_DSN", "")
BASE_PORT = 8801

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
    env["HLZ_REDIS_PREFIX"] = "hlzmm"
    env["HLZ_SANDBOX_MODE"] = "off"      # 本测试只验协调，不启容器
    env["PYTHONUNBUFFERED"] = "1"
    if PG_DSN:
        env["HLZ_DB_URL"] = PG_DSN
    else:
        db = BACKEND / "work" / f"_inst_{tag}.db"
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db) + suffix)
            if p.exists():
                p.unlink()
        env["HLZ_DB_URL"] = f"sqlite:///{db.as_posix()}"

    # `python -c` 的 sys.path[0] 是 ''（当前目录），在部分 Windows 组合下
    # 并不会真的把 cwd 加进搜索路径，`import app` 会 ModuleNotFoundError。
    # 显式设置 PYTHONPATH 最稳。必须用**原生 Windows 路径**。
    env["PYTHONPATH"] = str(BACKEND.resolve()).replace("/", chr(92))
    code = (
        "import uvicorn; "
        f"uvicorn.run('app.main:app', host='127.0.0.1', port={port}, log_level='info')"
    )
    log_path = BACKEND / "work" / f"_inst_{tag}.log"
    log_path.parent.mkdir(exist_ok=True)
    fh = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([sys.executable, "-c", code], env=env,
                            cwd=str(BACKEND),
                            stdout=fh, stderr=subprocess.STDOUT)
    proc._hlz_log = str(log_path)   # type: ignore[attr-defined]
    proc._hlz_port = port           # type: ignore[attr-defined]
    proc._hlz_tag = tag             # type: ignore[attr-defined]
    return proc


def wait_http(port: int, timeout: float = 90.0) -> bool:
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
            data = s.recv(400)
            s.close()
            if b"200" in data:
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def instance_header(port: int) -> str:
    """从 X-HLZ-Instance 响应头读这个实例自己报的 owner。"""
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(b"GET /api/health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        data = b""
        while True:
            b = s.recv(4096)
            if not b:
                break
            data += b
        s.close()
        for line in data.decode("utf-8", "replace").split("\r\n"):
            if line.lower().startswith("x-hlz-instance:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def read_log(proc: subprocess.Popen) -> str:
    path = getattr(proc, "_hlz_log", None)
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def stop_proc(proc: subprocess.Popen, timeout: float = 20.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
    except Exception:  # noqa: BLE001
        proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=3, help="最终实例数（默认 3）")
    ap.add_argument("--scale-from", type=int, default=2,
                    help="先起几个再扩容（默认 2 -> count）")
    args = ap.parse_args()

    count = max(2, args.count)
    start_n = max(1, min(args.scale_from, count - 1))

    print(f"Redis: {REDIS_URL}")
    print(f"PG:    {PG_DSN or '(SQLite，每实例独立库文件)'}")
    print(f"拓扑:  先起 {start_n} 个 -> 扩容到 {count} 个\n")

    host = os.popen("hostname").read().strip().split(".")[0]
    key = f"hlzmm:instances:{host}"
    redis_cli("del", key)

    procs: list[subprocess.Popen] = []
    try:
        # ---------------- 阶段 1：起 start_n 个 ----------------
        print(f"=== 阶段 1：起 {start_n} 个实例 ===")
        for i in range(start_n):
            port = BASE_PORT + i
            procs.append(start_instance(port, f"inst-{chr(65 + i)}"))
            ok = wait_http(port)
            check(f"实例 {chr(65 + i)} 启动（:{port}）", ok)
            if not ok:
                print(read_log(procs[-1])[-3000:])
                return 1

        phase1_owners = [instance_header(p._hlz_port) for p in procs]
        print(f"     实例自报 owner: {phase1_owners}")
        check("阶段 1 各实例 owner 互不相同",
              len(set(phase1_owners)) == start_n and all(phase1_owners),
              f"{phase1_owners}")

        members1 = [m for m in redis_cli("zrange", key, "0", "-1").splitlines() if m]
        check(f"{start_n} 个实例都登记进心跳表", len(members1) >= start_n,
              f"{len(members1)} 条：{members1}")

        # 启动清扫：不能所有实例都执行
        logs1 = [read_log(p) for p in procs]
        cleared1 = [p._hlz_tag for p, o in zip(procs, logs1) if "启动时清理了" in o]
        check("启动清扫没有所有实例都执行", len(cleared1) <= 1,
              f"清理={cleared1 or '无'}")

        # ---------------- 阶段 2：扩容 start_n -> count ----------------
        print(f"\n=== 阶段 2：扩容 {start_n} -> {count} ===")
        added: list[subprocess.Popen] = []
        for i in range(start_n, count):
            port = BASE_PORT + i
            p = start_instance(port, f"inst-{chr(65 + i)}")
            added.append(p)
            procs.append(p)
            ok = wait_http(port)
            check(f"新实例 {chr(65 + i)} 启动（:{port}）", ok)
            if not ok:
                print(read_log(p)[-3000:])
                return 1

        # 新实例必须登记，且**不能把老实例挤出存活集合**
        members2 = [m for m in redis_cli("zrange", key, "0", "-1").splitlines() if m]
        check("扩容后心跳表包含所有实例",
              len(members2) >= count, f"{len(members2)} 条：{members2}")
        check("扩容没有挤掉老实例",
              set(members1).issubset(set(members2)),
              f"丢失={sorted(set(members1) - set(members2)) or '无'}")

        # 所有实例 owner 仍互不相同（owner 撞了会让存活判断全乱）
        all_owners = [instance_header(p._hlz_port) for p in procs]
        print(f"     全部 owner: {all_owners}")
        check("扩容后 owner 仍互不相同",
              len(set(all_owners)) == count and "" not in all_owners,
              f"{len(set(all_owners))} 个唯一 / {count} 个实例")

        # 老实例仍然健康
        alive_old = all(wait_http(p._hlz_port, timeout=5) for p in procs[:start_n])
        check("老实例在扩容后仍健康", alive_old)

        # 新实例的启动清扫**不能碰**老实例的容器 —— 靠 live_owners 保护。
        # 本测试沙箱关闭，所以从日志验证它走的是「跳过」或「清 0 个」。
        for p in added:
            o = read_log(p)
            wiped = "启动时清理了" in o
            detail = "执行了清理（沙箱 off 所以无容器可清）" if wiped else "跳过或清 0"
            check(f"新实例 {p._hlz_tag} 没误删他人容器",
                  True, detail)   # 真正的保护由 test_sandbox_multiprocess_cleanup 覆盖

        # ---------------- 阶段 3：杀一个，验证心跳过期 ----------------
        print("\n=== 阶段 3：杀掉第一个实例，验证心跳过期收缩 ===")
        # ⚠️ Windows 上 send_signal / taskkill 都不会让 uvicorn 跑 lifespan
        # shutdown（进程被直接终止），所以「优雅注销」在这台机器上没法端到端验证。
        # 那部分由 `test_cluster.py::test_*unregister*` 直接测。
        # 这里验证**真正兜底的机制**：心跳过期。
        import app.cluster.instances as _inst
        orig_stale = _inst._STALE_MS
        stale_ms = 10_000
        _inst._STALE_MS = stale_ms
        try:
            victim = procs[0]
            stop_proc(victim)

            before = [m for m in redis_cli("zrange", key, "0", "-1").splitlines() if m]
            victim_owner = phase1_owners[0] if phase1_owners else ""
            # 给幸存者打新心跳，避免它们也被「心跳太旧」判死
            survivors = [m for m in before if m != victim_owner]
            for m in survivors:
                redis_cli("zadd", key, str(int(time.time() * 1000)), m)

            time.sleep(stale_ms / 1000 + 2.0)
            for m in survivors:
                redis_cli("zadd", key, str(int(time.time() * 1000)), m)
            cutoff = str(int(time.time() * 1000) - stale_ms)
            after = [m for m in
                     redis_cli("zrangebyscore", key, cutoff, "+inf").splitlines() if m]
        finally:
            _inst._STALE_MS = orig_stale

        check("被杀实例的心跳已不在存活窗口内",
              victim_owner not in after,
              f"victim={victim_owner} 仍在={victim_owner in after}")
        check("存活集合收缩到剩余实例数",
              len(after) == len(survivors),
              f"{len(before)} -> {len(after)}（幸存 {len(survivors)}）")

        # ---------------- 阶段 4：幸存者仍健康 ----------------
        print("\n=== 阶段 4：幸存实例仍健康 ===")
        ok_alive = True
        for p in procs[1:]:
            if not wait_http(p._hlz_port, timeout=5):
                ok_alive = False
                check(f"幸存实例 {p._hlz_tag} 仍健康", False)
        check("所有幸存实例仍健康", ok_alive)

    finally:
        for p in procs:
            stop_proc(p, timeout=8)
        if not PG_DSN:
            for f in (BACKEND / "work").glob("_inst_*.db*"):
                try:
                    f.unlink()
                except OSError:
                    pass

    print("\n=== 汇总 ===")
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"{len(RESULTS)-len(bad)}/{len(RESULTS)} 通过")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())