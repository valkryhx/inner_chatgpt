"""验证 exec_python 超时后，容器内进程真的被杀掉（不只是抛异常）。

历史：第 2 步只验证了「超时 2063ms 被捕获且容器存活」，
**没验证那个进程死没死**。如果它没死，就是「超时了但还在烧 CPU」——
比不超时更糟，因为调用方已经放弃等待、以为结束了。

这条测的就是 AGENTS.md §8.5「判定生效/送达的硬信号」：
不能只看「返回了 SandboxTimeout」，要看下游状态真的变了。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

COUNT_SRC = (
    "import os\n"
    "me = os.getpid()\n"
    "n = 0\n"
    "for pid in os.listdir('/proc'):\n"
    "    if not pid.isdigit() or int(pid) == me:\n"
    "        continue\n"
    "    try:\n"
    "        parts = open('/proc/' + pid + '/cmdline', 'rb').read().split(b'\\x00')\n"
    "    except OSError:\n"
    "        continue\n"
    "    if any(p.startswith(b'/ws/') for p in parts):\n"
    "        n += 1\n"
    "print(n, flush=True)\n"
)


async def main() -> int:
    from app.sandbox.config import get_config
    from app.sandbox.runtime import ContainerRuntime
    from app.sandbox.errors import SandboxTimeout
    from app.config import SANDBOX_WORKSPACES_DIR
    import docker

    cfg = get_config(reload=True)
    rt = ContainerRuntime(cfg)
    uid = "u_c56b8337dd09"
    ws = Path(SANDBOX_WORKSPACES_DIR) / uid
    ws.mkdir(parents=True, exist_ok=True)

    client = docker.from_env()
    name = "hlz_sbx_" + uid
    try:
        c = client.containers.get(name)
    except Exception:
        cid = rt.create(uid, str(ws))
        c = client.containers.get(cid)

    def count() -> int:
        r = c.exec_run(["python", "-c", COUNT_SRC], demux=True)
        out = (r.output[0] or b"").decode(errors="replace").strip()
        return int(out) if out.isdigit() else -1

    print(f"起始进程数 = {count()}")

    # 跑一个 60s 的 sleep，超时设为 3s
    code = "import time\nprint('START', flush=True)\ntime.sleep(60)\nprint('DONE')\n"
    t0 = time.time()
    timed_out = False
    try:
        res = await rt.exec_python(c.id, code, timeout_s=3.0, host_workspace=str(ws))
        print(f"没有超时？exit={res.exit_code} out={res.stdout[:80]!r}")
    except SandboxTimeout:
        timed_out = True
        print(f"超时被捕获，耗时 {time.time()-t0:.2f}s")

    # 关键：等一会儿，确认进程真的没了（不是杀完又活着）
    for i in range(10):
        await asyncio.sleep(1)
        n = count()
        if n == 0:
            print(f"超时后 {i+1}s 进程数 = 0")
            break
    else:
        print(f"超时后 10s 进程数仍是 {n}")

    after = count()
    container_ok = c.status == "running"
    client.close()

    print("\n== 判定 ==")
    ok = timed_out and after == 0 and container_ok
    print(f"  [{'PASS' if timed_out else 'FAIL'}] SandboxTimeout 被抛出")
    print(f"  [{'PASS' if after == 0 else 'FAIL'}] 超时后容器内进程真的被杀 (剩 {after})")
    print(f"  [{'PASS' if container_ok else 'FAIL'}] 容器本身仍存活（超时不销毁容器）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))