"""code_run 的轻量写入边界。

这是进程内的防御层，不是操作系统级沙箱：目标是拦住模型/脚本常见的误写
（绝对路径、``..``、构建目录等），确保普通 Python 代码只能写用户 workspace。
真正的生产级隔离仍应由容器 / 独立账户 / OS sandbox 提供。
"""
from __future__ import annotations

import os
from pathlib import Path

PROTECTED_CODE_MARKERS = (
    "frontend/dist",
    "frontend/src",
)

# 通过 ``python -c`` 注入的运行器。先安装审计钩子，再执行用户脚本。
WORKSPACE_PYTHON_GUARD = r'''
import os
import runpy
import sys
from pathlib import Path

root = Path(sys.argv[2]).resolve()
script = sys.argv[1]
inner = os.environ.get("INNER_AGENT_WRITE_ROOT")
if inner:
    root = Path(inner).resolve()
sys.dont_write_bytecode = True

def _candidate(value):
    if isinstance(value, int):
        return None
    try:
        raw = os.fsdecode(value)
    except (TypeError, ValueError):
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        return p.resolve()
    except OSError:
        return Path(os.path.abspath(str(p)))

def _check(value, label="写入"):
    p = _candidate(value)
    if p is None:
        return
    try:
        p.relative_to(root)
    except ValueError:
        raise PermissionError(
            f"{label}越界：{p}；code_run 只能写当前 workspace {root}"
        ) from None

def _open_is_write(mode, flags):
    if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
        return True
    if isinstance(flags, int):
        bits = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        return bool(flags & bits)
    return False

def _audit(event, args):
    if event == "open":
        if len(args) >= 3 and _open_is_write(args[1], args[2]):
            _check(args[0])
    elif event in {"os.mkdir", "os.rmdir", "os.remove", "os.unlink", "os.truncate", "os.chmod", "os.utime"}:
        if args:
            _check(args[0])
    elif event in {"os.rename", "os.replace", "os.link"}:
        if len(args) >= 2:
            _check(args[0], "移动/链接")
            _check(args[1], "移动/链接")
    elif event == "os.symlink":
        if len(args) >= 2:
            _check(args[1], "创建链接")

sys.addaudithook(_audit)
sys.argv = [script] + sys.argv[3:]
runpy.run_path(script, run_name="__main__")
'''


def protected_code_reason(code: str) -> str | None:
    """Return a human-readable reason when code targets protected source paths."""
    normalized = (code or "").replace("\\", "/").lower()
    for marker in PROTECTED_CODE_MARKERS:
        if marker in normalized:
            return (
                f"禁止在 code_run 中访问 {marker}；"
                "所有产物必须写入当前 workspace，图片用 ![说明](相对路径.png) 展示"
            )
    return None


def workspace_env(cwd: Path) -> dict[str, str]:
    """子进程环境：临时文件也固定到 workspace 内。"""
    env = os.environ.copy()
    tmp = cwd / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env["TMP"] = str(tmp)
    env["TEMP"] = str(tmp)
    env["TMPDIR"] = str(tmp)
    env["INNER_AGENT_WRITE_ROOT"] = str(cwd.resolve())
    return env
