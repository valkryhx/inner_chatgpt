"""探针：destroy 之后到底剩下什么（pid / 状态 / 是不是僵尸）。

## 为什么要分辨「僵尸」和「活着」

第一次探针只输出 `orphans: 1`，我差点直接去改 kill 逻辑。实际上：
- **僵尸（Z）**：进程已经死了，只是父进程没 wait —— 占一个 PID 槽，几乎不占内存
- **活着（S/R）**：真的还在跑，`prlimit --as=2GB` 还压着，这才是泄漏

两者的修法完全不同：僵尸要补 `wait()`，活进程要改信号投递方式。
不先分辨就去改，很容易"修好了"一个不存在的问题（本仓库踩过）。

跑法（容器内）：
    docker cp docs/probes/20261119-daemon进程清理探针.py <容器>:/tmp/p.py
    docker exec <容器> python /tmp/p.py

期望 `VERDICT: CLEAN`。`ZOMBIE` 退出码 2（进程已死没 wait），
`LEAK` 退出码 1（活进程残留，真正的内存泄漏）。

> Windows 上 `docker cp` / `docker exec` 前必须 `MSYS_NO_PATHCONV=1`，
> 否则 Git Bash 把 `/tmp/p.py` 转成 `C:/Users/.../tmp/p.py`（实测踩过）。
> 本探针**不依赖 pytest**：生产镜像按设计不带测试依赖。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/app")

USER = "u_orphan_probe2"


def proc_rows(user: str) -> list[dict]:
    """属于该 user 的 bwrap 行（含状态）。"""
    out = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,stat=,etimes=,args="], capture_output=True, text=True
    ).stdout
    rows = []
    for line in out.splitlines():
        if f"--bind /app/backend/data/workspaces/{user} /ws" not in line:
            continue
        pid, ppid, stat, etimes = line.split(None, 4)[:4]
        rows.append(
            {
                "pid": int(pid),
                "ppid": int(ppid),
                "stat": stat,
                "etimes": int(etimes),
                "is_bwrap": line.split(None, 4)[4].startswith("bwrap"),
            }
        )
    return rows


def rss_kb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


async def main() -> int:
    from sandbox_service.executor.bwrap import BwrapExecutor
    from sandbox_service.workspace import make_resolver

    ex = BwrapExecutor(workspace_resolver=make_resolver())
    print(f"pid_of_this_probe={os.getpid()}")

    ok = await ex.start(USER)
    print(f"start ok={ok}")
    if not ok:
        print("SKIP: 预热失败")
        return 0

    daemon = ex._daemons[USER]
    print(f"daemon pid(bwrap)={daemon.proc.pid}")
    print("预热后:", proc_rows(USER))

    await ex.exec(USER, "print('hello')")
    before = proc_rows(USER)
    print("执行后:", before)
    print("  总进程:", len(before), " 总 RSS KB:", sum(rss_kb(r["pid"]) for r in before))

    await ex.destroy(USER)
    print(f"destroy 返回，proc.returncode={daemon.proc.returncode}")
    await asyncio.sleep(2.0)

    after = proc_rows(USER)
    print("销毁后:", after)
    zombies = [r for r in after if r["stat"].startswith("Z")]
    alive = [r for r in after if not r["stat"].startswith("Z")]
    print(f"  僵尸: {len(zombies)}  活进程: {len(alive)}")
    for r in alive:
        print(f"  存活 pid={r['pid']} stat={r['stat']} 年龄={r['etimes']}s RSS={rss_kb(r['pid'])}KB")
    for r in zombies:
        print(f"  僵尸 pid={r['pid']} ppid={r['ppid']}")

    # 判定：**活进程**才算泄漏；僵尸也算问题（PID 槽泄漏），单独报
    if alive:
        print("VERDICT: LEAK（有活进程）")
        return 1
    if zombies:
        print("VERDICT: ZOMBIE（进程已死但没 wait）")
        return 2
    print("VERDICT: CLEAN")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))