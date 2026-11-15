"""多 worker（`workers=2`）启动探测：uvicorn 能不能真的起两个 worker。

## 为什么需要它

部署是单 worker，但「能不能改多 worker」这个问题会反复被问到，
而多 worker 会动摇几个**进程内**假设：

- `compaction_lock` / `_pending` 赛后压缩表 / `turn_job._JOBS` 都是**进程内**状态；
- 记忆 dream 的锁改用 DB 行（`lock_pid` / `lock_at`）正是为了跨进程，
  但 `recover_dream_state()` 在 lifespan 里被**每个 worker 各调一次**。

本探针只回答第一步：**多 worker 能不能起来**。起来了才谈得上后面的一致性验证。

## 两个真实的坑（2026-11-15 都踩到）

1. **启动脚本位置**：若写在 `work/`，`sys.path[0]` 就是 `work/`；
   `workers=2` 走 multiprocessing **spawn**，子进程继承 `sys.path`，
   于是 `import app` 直接 `ModuleNotFoundError`。表现为「父进程健康、
   子进程全崩」，而父进程一时还回 200，很容易被误判成「多 worker 正常」。
   **修法**：脚本必须落在 `backend/` 里。

2. **Windows 上 `terminate()` 杀不干净**：它只终止直接子进程，
   uvicorn 的 worker 是孙进程，会残留。而且父进程在无终端环境里可能
   一直不退出，`communicate(timeout=...)` 之后进程还挂着。
   **修法**：`taskkill /F /T`（`/T` 连子进程树一起），再兜底 `kill()`。

## 跑法

    python tools/turn_job_verify/probe_multiworker.py    # 约 30-60s
"""
from __future__ import annotations

import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# tools/turn_job_verify/ 下：parents[2] 是仓库根
ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

# 整体墙钟：uvicorn workers 在 Windows 上偶发卡在 spawn，不能无限等。
TOTAL_BUDGET_S = 90


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hard_kill(proc: subprocess.Popen) -> None:
    """连子进程树一起杀。Windows 上 `terminate()` 只杀直接子进程。"""
    if proc.poll() is not None:
        return
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001 - taskkill 不可用就退回 kill
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    port = free_port()
    # 关键：脚本放在 backend/ 下，spawn 出来的子进程 sys.path[0] 才是 backend/
    boot = BACKEND / "_probe_mw_boot.py"
    boot.write_text(
        "import uvicorn\n"
        "if __name__ == '__main__':\n"
        f"    uvicorn.run('app.main:app', host='127.0.0.1', port={port},"
        " workers=2, reload=False, log_level='info')\n",
        encoding="utf-8", newline="\n")

    started = time.monotonic()
    print(f"启动 2 个 worker，端口 {port}（预算 {TOTAL_BUDGET_S}s）")

    # 输出写到文件而不是 PIPE：PIPE 缓冲满了会把子进程阻塞死，
    # 这是上次探针「看起来卡住」的原因之一。
    log_path = ROOT / "work" / "_mw_probe.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, str(boot)], cwd=str(BACKEND),
        stdout=log_f, stderr=subprocess.STDOUT)

    healthy = False
    try:
        while time.monotonic() - started < TOTAL_BUDGET_S:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                    if r.status == 200:
                        healthy = True
                        break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        # 给 worker 一点时间把 "Started server process" 写进日志
        time.sleep(3)
    finally:
        _hard_kill(proc)
        try:
            log_f.close()
        except Exception:  # noqa: BLE001
            pass
        boot.unlink(missing_ok=True)

    out = ""
    try:
        out = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    pids = re.findall(r"Started server process \[(\d+)\]", out)
    elapsed = time.monotonic() - started
    print(f"健康检查通过: {healthy}（耗时 {elapsed:.1f}s）")
    print(f"worker 进程号: {pids}")

    two_workers = len(pids) >= 2
    print()
    print(f"[{'OK  ' if healthy else 'FAIL'}] M-1 多 worker 能起来并响应 /api/health")
    print(f"[{'OK  ' if two_workers else 'FAIL'}] M-2 确实起了 2 个 worker 进程"
          f"（看到 {len(pids)} 个）")
    if healthy and not two_workers:
        print("  注意：健康检查通过但只有一个进程 —— 可能 uvicorn 降级成单进程，")
        print("        或子进程崩了但父进程仍在服务（看下面尾部输出）")
    if out[-600:].strip():
        print("\n--- 尾部输出 ---")
        print(out[-600:])
    print(f"\n完整日志：{log_path}")
    return 0 if (healthy and two_workers) else 1


if __name__ == "__main__":
    sys.exit(main())
