"""直接验证 `stop_user` 的 pkill 能杀掉真实长跑进程。

不走 LLM：直接起一个 300s 的 python 进程（用 runtime 的正常路径写脚本 +
exec），确认它活着，然后调 `exec_shell_sync_killall`，确认它死了。

这条是 `/stop` 级联的物理前提：如果 pkill 模式匹配不上真实进程，
接口返回 ok 也是假的。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))


async def main() -> int:
    from app.sandbox.config import get_config
    from app.sandbox.runtime import ContainerRuntime
    from app.config import SANDBOX_WORKSPACES_DIR

    cfg = get_config(reload=True)
    rt = ContainerRuntime(cfg)

    uid = "u_c56b8337dd09"
    ws = Path(SANDBOX_WORKSPACES_DIR) / uid
    ws.mkdir(parents=True, exist_ok=True)

    import docker
    client = docker.from_env()
    name = "hlz_sbx_" + uid
    try:
        c = client.containers.get(name)
    except Exception:
        print(f"容器 {name} 不存在，先创建")
        cid = rt.create(uid, str(ws))
        c = client.containers.get(cid)

    def count_long() -> int:
        """数容器内的长跑进程。

        ⚠️ 不能用 ``ps``：``python:3.12-slim`` 没有 procps（实测 `which ps` 空）。
        改用 /proc 遍历，与 runtime 的实现同源。
        """
        script = (
            "import os\n"
            "me = os.getpid()\n"
            "n = 0\n"
            "for pid in os.listdir('/proc'):\n"
            "    if not pid.isdigit() or int(pid) == me:\n"
            "        continue\n"
            "    try:\n"
            "        parts = open('/proc/' + pid + '/cmdline','rb').read().split(b'\\x00')\n"
            "    except OSError:\n"
            "        continue\n"
            "    # 任一 argv 以 /ws/ 开头才算业务进程（真实调用是 python -u /ws/x.py）\n"
            "    if any(p.startswith(b'/ws/') for p in parts):\n"
            "        n += 1\n"
            "print(n, flush=True)\n"
        )
        r = c.exec_run(["python", "-c", script], demux=True)
        out = (r.output[0] or b"").decode(errors="replace").strip()
        return int(out) if out.isdigit() else -1

    print(f"容器 {name} 状态 {c.status}")

    # 起一个 300s 长跑（异步，不等它结束）
    marker = "stoptest"
    host_dir = ws / ".hlz_tmp"
    host_dir.mkdir(exist_ok=True)
    (host_dir / f"_hlz_{marker}.py").write_text(
        "import time\nprint('LONG_STARTED', flush=True)\ntime.sleep(300)\nprint('LONG_DONE')\n",
        encoding="utf-8", newline="\n")

    async def launcher():
        # 用 exec_python 会等结果；这里直接 raw exec 起后台
        await asyncio.to_thread(
            c.exec_run,
            ["sh", "-c", f"nohup python -u /ws/.hlz_tmp/_hlz_{marker}.py >/dev/null 2>&1 &"])

    await launcher()
    await asyncio.sleep(2)

    before = count_long()
    print(f"启动后 长跑进程数 = {before}")
    if before <= 0:
        print("FAIL: 长跑进程没起来")
        client.close()
        return 1

    # 调级联用的 killall
    t0 = time.time()
    rt.exec_shell_sync_killall(c.id)
    await asyncio.sleep(1.5)
    after = count_long()
    print(f"killall 后 长跑进程数 = {after}  （耗时 {time.time()-t0:.2f}s）")

    try:
        (host_dir / f"_hlz_{marker}.py").unlink()
    except OSError:
        pass
    client.close()

    print("\n== 判定 ==")
    if after == 0:
        print("  [PASS] stop_user 的 pkill 能杀掉真实长跑进程")
        return 0
    print(f"  [FAIL] 仍有 {after} 个进程存活")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))