"""实证：多智能体时 write_root 与容器挂载点是否对得上。

场景：
  容器按某个 host_workspace 建好，把那个目录挂到 /ws。
  之后 exec_python(host_workspace=X) 会把脚本写到 X/.hlz_tmp/，
  但容器里执行的是 /ws/.hlz_tmp/xxx.py。

  所以只有当 X 恰好等于「建容器时挂的那个目录」时，脚本才找得到。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

os.environ["HLZ_SANDBOX_MVP_USERS"] = "u_probe"

from app.sandbox.manager import _host_workspace  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    import app.workspace as wsmod
    wsmod.SANDBOX_WORKSPACES_DIR = tmp
    wsmod.SANDBOX_WORKSPACE_USERS = ("u_probe",)

    root_ws = _host_workspace("u_probe", "")
    a1_ws = _host_workspace("u_probe", ".subagents/sess/a1")

    print(f"主 agent 挂载目录 : {root_ws}")
    print(f"子 agent 挂载目录 : {a1_ws}")
    print()

    # 模拟：容器按主 agent 的目录建（挂到 /ws）
    container_mount = Path(root_ws)
    # 模拟：子 agent 执行时把脚本写到自己目录的 .hlz_tmp
    sub_tmp = Path(a1_ws) / ".hlz_tmp"
    sub_tmp.mkdir(parents=True, exist_ok=True)
    script = sub_tmp / "_hlz_test.py"
    script.write_text("print(1)", encoding="utf-8")

    # 容器内看到的是 /ws -> container_mount
    inside_view = container_mount / ".hlz_tmp" / "_hlz_test.py"

    print(f"脚本实际写在   : {script}")
    print(f"容器内会去找   : {inside_view}")
    print(f"两者同一文件？ : {script.resolve() == inside_view.resolve()}")
    print()

    ok = script.resolve() == inside_view.resolve()
    if not ok:
        print("[BUG] 子 agent 的脚本写到容器看不到的位置 -> ExecError 文件不存在")
    else:
        print("[OK] 挂载点一致")

    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())