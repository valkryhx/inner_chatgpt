"""测沙箱容器的真实资源占用，用于推算 N 个用户的容量。

不是跑 `docker stats` 看一眼就完 —— 那只能看到「空闲」这一档。
真正决定容量的是**峰值**：模型跑 pandas/matplotlib 时能吃多少。

测四档：
  1. 空闲（sleep infinity）
  2. 跑过 python 后（解释器已加载）
  3. 跑过 pandas 后（最大头）
  4. 分配大数组逼近上限

同时测 Docker Desktop 的固定开销（WSL2 vhdx 基线）。

用法：
    python tests/manual/resource_profile.py --n 5
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

IMAGE = "hlz-sandbox:2026-11-18"


def run(cmd: list[str], timeout: int = 120) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def mem_mib(name: str) -> float:
    """容器内存占用（MiB），读 cgroup v2 的 memory.current。

    ⚠️ 关键：必须在**进程还活着的时候**测。
    第一版在 `docker exec python -c "..."` 返回后才读，那时进程已退出、
    内存已释放，测到的永远是 idel 的 2~3 MiB —— 完全错误的数字。
    正确做法是把负载放到后台跑（`docker exec -d` + 内部 sleep 保持），
    在它存活期间采样。
    """
    r = subprocess.run(
        ["docker", "exec", name, "sh", "-c",
         "cat /sys/fs/cgroup/memory.current 2>/dev/null || "
         "cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null || echo 0"],
        capture_output=True, text=True, timeout=30)
    raw = (r.stdout or "0").strip()
    try:
        return int(raw) / 1024 / 1024
    except ValueError:
        return -1.0


def exec_background(name: str, code: str, keep_s: int = 45) -> None:
    """后台执行一段 Python，进程存活 keep_s 秒后自己退出。

    用 `docker exec -d`（detached）—— 这样 exec 立刻返回，
    而 python 进程继续跑，我们就能在它活着时采到峰值内存。
    """
    wrapped = code + f"\nimport time as _t; _t.sleep({keep_s})\n"
    subprocess.run(["docker", "exec", "-d", name, "python", "-u", "-c", wrapped],
                   capture_output=True, text=True, timeout=60)


def cpu_seconds(name: str) -> float:
    r = subprocess.run(
        ["docker", "exec", name, "sh", "-c",
         "cat /sys/fs/cgroup/cpu.stat 2>/dev/null | head -1 || echo 'usage_usec 0'"],
        capture_output=True, text=True, timeout=30)
    raw = (r.stdout or "").strip()
    try:
        return int(raw.split()[1]) / 1_000_000
    except (IndexError, ValueError):
        return -1.0


def host_free_gib() -> float:
    """宿主可用物理内存（GiB）—— 用 PowerShell CIM，避免 psutil 依赖。"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "$o=Get-CimInstance Win32_OperatingSystem; "
         "[math]::Round($o.FreePhysicalMemory/1MB,2)"],
        capture_output=True, text=True, timeout=60)
    try:
        return float((r.stdout or "0").strip())
    except ValueError:
        return -1.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5, help="并发容器数")
    a = ap.parse_args()

    n = a.n
    print(f"镜像 {IMAGE}，并发 {n} 个容器")
    print(f"宿主可用内存（起容器前）: {host_free_gib():.2f} GiB\n")

    names = [f"hlz_prof_{i}" for i in range(n)]
    for nm in names:
        run(["docker", "rm", "-f", nm], timeout=60)

    t0 = time.perf_counter()
    for nm in names:
        run(["docker", "run", "-d", "--name", nm,
             "--network", "none", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges",
             "--memory", "1g", "--memory-swap", "1g",
             "--cpus", "1", "--pids-limit", "256",
             "--tmpfs", "/tmp:size=256m,mode=1777",
             "-w", "/ws", IMAGE, "sleep", "infinity"], timeout=180)
    create_all = time.perf_counter() - t0
    print(f"创建 {n} 个容器总耗时: {create_all:.1f}s  （平均 {create_all/n*1000:.0f} ms/个）")

    time.sleep(3)
    rows = []

    def snapshot(label: str) -> None:
        vals = [mem_mib(nm) for nm in names]
        good = [v for v in vals if v >= 0]
        if not good:
            print(f"  {label}: 读取失败")
            return
        avg = sum(good) / len(good)
        rows.append({"stage": label, "avg_mib": round(avg, 2),
                     "max_mib": round(max(good), 2), "n": len(good)})
        print(f"  {label}: 平均 {avg:.2f} MiB，最大 {max(good):.2f} MiB")

    print("\n== 分档内存（都在进程存活期间采样）==")
    snapshot("1_空闲")

    # 2. python 解释器加载后
    for nm in names:
        exec_background(nm, "print('py-ready')", keep_s=20)
    time.sleep(6)
    snapshot("2_跑过python")
    time.sleep(18)

    # 3. import pandas
    for nm in names:
        exec_background(nm, "import pandas\nprint(pandas.__version__)", keep_s=25)
    time.sleep(12)
    snapshot("3_import_pandas")
    time.sleep(20)

    # 4. 分配 ~300MB 数组（真实数据处理量级）
    for nm in names:
        exec_background(nm,
                        "import numpy as np\n"
                        "a = np.ones((400, 1024, 100), dtype=np.float64)\n"
                        "print(a.nbytes)", keep_s=30)
    time.sleep(14)
    snapshot("4_分配300MB")
    time.sleep(22)

    # 5. pandas 读 CSV + 分组聚合（最常见的数据任务形态）
    for nm in names:
        exec_background(nm,
                        "import pandas as pd, numpy as np\n"
                        "df = pd.DataFrame({'k': np.random.randint(0,50,300000),\n"
                        "                   'v': np.random.rand(300000)})\n"
                        "g = df.groupby('k')['v'].agg(['mean','sum','count'])\n"
                        "print(len(g))", keep_s=30)
    time.sleep(16)
    snapshot("5_pandas聚合")
    time.sleep(20)

    # 5. 矩阵运算（CPU 峰值，取 cpu.stat 增量）
    print("\n== CPU（单容器单核跑满）==")
    before = cpu_seconds(names[0])
    t0 = time.perf_counter()
    run(["docker", "exec", names[0], "python", "-u", "-c",
         "import numpy as np\n"
         "a=np.random.rand(1200,1200); b=np.random.rand(1200,1200)\n"
         "for _ in range(6): a@b\n"
         "print('done')"], timeout=600)
    wall = time.perf_counter() - t0
    after = cpu_seconds(names[0])
    used = after - before
    print(f"  矩阵乘法 6 次: 墙钟 {wall:.1f}s，CPU 时间 {used:.1f}s，"
          f"利用率 {used/wall*100:.0f}%")

    print("\n== 宿主内存 ==")
    free = host_free_gib()
    print(f"  起 {n} 个容器后可用: {free:.2f} GiB")

    # 清理
    print("\n== 清理 ==")
    for nm in names:
        run(["docker", "rm", "-f", nm], timeout=60)
    print(f"  已销毁 {n} 个")

    out = Path(__file__).resolve().parents[2] / "work" / "resource_profile.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "n": n, "image": IMAGE,
        "create_total_s": round(create_all, 1),
        "create_avg_ms": round(create_all / n * 1000),
        "stages": rows,
        "host_free_after_gib": free,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n数据已存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())