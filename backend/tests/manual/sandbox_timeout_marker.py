"""验证超时路径 `_pkill_marker_sync` 的 /proc/environ 匹配是否真的生效。

这是与 `exec_shell_sync_killall` 不同的另一条杀进程路径：
- killall 走 `/proc/<pid>/cmdline`（匹配 argv 以 /ws/ 开头）
- marker 走 `/proc/<pid>/environ`（匹配环境变量 HLZ_EXEC_MARKER=<marker>）

后者只被 `_exec` 超时分支调用。如果它失效，表现为「超时后返回了
SandboxTimeout，但容器里那个进程还在继续跑」——又一个静默假信号。

本脚本直接构造：起一个带 marker 的长跑进程 → 调 _pkill_marker_sync → 确认死掉。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

COUNT_STARTING_POINT = (
    "import os\n"
    "me = os.getpid()\n"
    "n = 0\n"
    "for pid in os.listdir('/proc'):\n"
    "    if not pid.isdigit() or int(pid) == me:\n"
    "        continue\n"
    "    try:\n"
    "        env = open('/proc/' + pid + '/environ', 'rb').read()\n"
    "    except OSError:\n"
    "        continue\n"
    "    if needle in env:\n"
    "        n += 1\n"
    "print(n, flush=True)\n"
)


async def main() -> int:
    from app.sandbox.config import get_config
    from app.sandbox.runtime import ContainerRuntime
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
        print(f"容器 {name} 不存在，创建")
        cid = rt.create(uid, str(ws))
        c = client.containers.get(cid)

    marker = "mktest" + str(int(time.time()))

    def count_by_marker(m: str) -> int:
        script = f"needle = {f'HLZ_EXEC_MARKER={m}'.encode()!r}\n" + COUNT_STARTING_POINT
        r = c.exec_run(["python", "-c", script], demux=True)
        out = (r.output[0] or b"").decode(errors="replace").strip()
        return int(out) if out.isdigit() else -1

    # 起一个带 marker 的长跑进程（模拟 _exec 的 environment= 注入）
    host_dir = ws / ".hlz_tmp"
    host_dir.mkdir(exist_ok=True)
    script_path = host_dir / f"_hlz_{marker}.py"
    script_path.write_text(
        "import time\nprint('LONG_STARTED', flush=True)\ntime.sleep(300)\n",
        encoding="utf-8", newline="\n")

    await asyncio.to_thread(
        c.exec_run,
        ["sh", "-c",
         f"HLZ_EXEC_MARKER={marker} nohup python -u /ws/.hlz_tmp/_hlz_{marker}.py "
         f">/dev/null 2>&1 &"],
        environment={"HLZ_EXEC_MARKER": marker})

    await asyncio.sleep(2)
    before = count_by_marker(marker)
    print(f"启动后 匹配 marker 的进程数 = {before}")
    if before <= 0:
        print("FAIL: 进程没起来，或 /proc/environ 里读不到 marker")
        # 诊断：打印该进程的 environ 片段
        r = c.exec_run(["sh", "-c",
                        "for p in /proc/[0-9]*; do "
                        "tr '\\0' '\\n' < $p/environ 2>/dev/null | grep -l HLZ_EXEC_MARKER "
                        ">/dev/null && echo $p; done"], demux=True)
        print("  诊断输出:", (r.output[0] or b"").decode(errors="replace")[:300])
        client.close()
        return 1

    # 调超时路径用的精确杀
    rt._pkill_marker_sync(c.id, marker)
    await asyncio.sleep(1.5)
    after = count_by_marker(marker)
    print(f"_pkill_marker_sync 后 匹配 marker 的进程数 = {after}")

    try:
        script_path.unlink()
    except OSError:
        pass
    client.close()

    print("\n== 判定 ==")
    if after == 0:
        print("  [PASS] /proc/environ 匹配能精确杀掉带 marker 的进程")
        return 0
    print(f"  [FAIL] 仍有 {after} 个进程存活 —— 超时后进程会继续跑")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))