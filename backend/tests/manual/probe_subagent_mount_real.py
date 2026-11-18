"""真机实证：子代理（带 write_root）在沙箱里执行是否真的失败。

不复现整个多智能体链路（那要跑 LLM），只复现**挂载层**：
  1. 按主 agent 目录建容器（挂到 /ws）
  2. 让 exec_python 用子 agent 的 host_workspace 执行
  3. 看是否报「文件不存在」

如果失败 -> 多智能体在沙箱里根本跑不通，必须修。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import shutil
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

os_users = "u_mounttest"


async def main() -> int:
    from app.sandbox.config import get_config
    from app.sandbox.runtime import ContainerRuntime, container_name
    from app.sandbox.errors import SandboxExecError, SandboxUnavailable
    import docker

    cfg = get_config(reload=True)
    rt = ContainerRuntime(cfg)

    tmp = Path(tempfile.mkdtemp(prefix="hlz_mount_"))
    root_ws = tmp / os_users
    sub_ws = root_ws / ".subagents" / "sess" / "a1"
    sub_ws.mkdir(parents=True, exist_ok=True)

    cid = None
    client = docker.from_env()
    try:
        # 1) 按主 agent 目录建容器
        cid = rt.create(os_users, str(root_ws))
        print(f"容器已建，挂载 {root_ws} -> /ws")

        # 2) 主 agent 执行（应该成功）
        r = await rt.exec_python(cid, "print('MAIN_OK')", timeout_s=30,
                                 host_workspace=str(root_ws),
                                 container_workspace=str(root_ws))
        print(f"主 agent: {r.stdout.strip()!r} exit={r.exit_code}")

        # 3) 子 agent 执行（write_root 不同，但挂载根仍是 root_ws）
        try:
            r2 = await rt.exec_python(cid, "print('SUB_OK')", timeout_s=30,
                                      host_workspace=str(sub_ws),
                                      container_workspace=str(root_ws),
                                      cwd='/ws/.subagents/sess/a1')
            print(f"子 agent: {r2.stdout.strip()!r} exit={r2.exit_code}")
            sub_ok = "SUB_OK" in r2.stdout
        except SandboxExecError as exc:
            print(f"子 agent: SandboxExecError -> {exc}")
            sub_ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"子 agent: {type(exc).__name__} -> {exc}")
            sub_ok = False

        # 4) 再验证：子 agent 写的文件落在自己的子目录，主 agent 目录不留痕
        await rt.exec_python(cid, "open('sub_only.txt','w').write('x')", timeout_s=30,
                             host_workspace=str(sub_ws),
                             container_workspace=str(root_ws),
                             cwd='/ws/.subagents/sess/a1')
        sub_file = sub_ws / "sub_only.txt"
        root_file = root_ws / "sub_only.txt"
        isolate_ok = sub_file.exists() and not root_file.exists()
        print(f"子目录隔离: 子目录有={sub_file.exists()} 根目录没有={not root_file.exists()}")

        print()
        if sub_ok and isolate_ok:
            print("[OK] 子 agent 能执行，且写入隔离到自己的子目录")
            return 0
        if not sub_ok:
            print("[BUG] 子 agent 执行失败 —— 多智能体在沙箱里跑不通")
        if not isolate_ok:
            print("[BUG] 子 agent 的文件没有隔离到自己的子目录")
        return 1
    except SandboxUnavailable as exc:
        print(f"沙箱不可用，跳过：{exc}")
        return 2
    finally:
        if cid:
            rt.destroy(cid)
        try:
            client.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))