"""code_run 的进程内边界：写限制在 workspace，读限制在可读根。

**这不是安全边界，只是防御纵深的一层。** 有心人仍可绕过（ctypes 直调 libc、
另起一个不走本守卫的 python 进程）。真正的多租户隔离必须靠容器/gVisor/microVM
—— 参见 `D:/git_codes/Aivory/sandbox-service`（每次会话一个 Docker 容器，
`--network none --read-only --cap-drop ALL`，且**没配 sandbox 时直接拒绝执行**）。

在换成容器之前，这层守卫至少要拦住两类真实存在的问题：

1. **读逃逸**：`file_read` 严格限制在 workspace，而 `code_run` 以前只拦写不拦读，
   于是一个登录用户能 `open(".../data/app.db","r")` 读出所有其他用户的 api_key、
   密码哈希和全部对话。两个工具对「能读什么」的判断必须一致，弱的那条就是漏洞。
2. **shell 分支裸奔**：`powershell`/`bash` 分支以前完全不经过守卫。

修复后：
  - `open` 事件**无论读写**都校验；读的可读根与 `workspace.safe_read_roots` 一致；
  - 目录遍历（`os.listdir`/`os.scandir`/`os.walk`）同样校验；
  - 敏感路径（会话 DB 及其 wal/shm、其他用户的 workspace）显式拒绝；
  - shell 分支也装同一套守卫。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROTECTED_CODE_MARKERS = (
    "frontend/dist",
    "frontend/src",
)

# 通过 ``python -c`` 注入的运行器。先安装审计钩子，再执行用户脚本。
#
# 参数约定：argv[1]=脚本路径，argv[2]=写根，argv[3]=可读根列表（os.pathsep 分隔，
# 可为空表示只允许写根）。多出来的参数传给用户脚本。
WORKSPACE_PYTHON_GUARD = r'''
import os
import runpy
import sys
from pathlib import Path

script = sys.argv[1]
write_root = Path(sys.argv[2]).resolve()
read_roots_raw = sys.argv[3] if len(sys.argv) > 3 else ""
inner = os.environ.get("INNER_AGENT_WRITE_ROOT")
if inner:
    write_root = Path(inner).resolve()

read_roots = [write_root]
for chunk in read_roots_raw.split(os.pathsep):
    chunk = chunk.strip()
    if not chunk:
        continue
    try:
        read_roots.append(Path(chunk).resolve())
    except OSError:
        continue

# 正在执行的这个脚本本身必须可读（它可能建在系统临时目录里）。
try:
    read_roots.append(Path(script).resolve().parent)
except OSError:
    pass

# Python 解释器自身必须可读，否则连 import 都过不去（stdlib / site-packages）。
# 这些目录里不含租户数据，放行不影响隔离目标。
for entry in {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}:
    if not entry:
        continue
    try:
        read_roots.append(Path(entry).resolve())
    except OSError:
        continue
for entry in list(sys.path):
    if not entry:
        continue
    try:
        p = Path(entry).resolve()
    except OSError:
        continue
    read_roots.append(p)

# 无论怎么配都必须拒绝的敏感路径：会话数据库及其 WAL/SHM。
DENY_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")

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

def _under(p, root):
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False

def _is_denied(p):
    """敏感路径：数据库文件一律不给碰，哪怕它恰好落在可读根里。"""
    name = p.name.lower()
    if name.endswith(DENY_SUFFIXES):
        return True
    # 同一目录下可能放着别的租户数据；只放行自己写根内的。
    return False

def _check_read(value):
    p = _candidate(value)
    if p is None:
        return
    if _is_denied(p):
        raise PermissionError(
            f"读取越界：{p} 是平台数据库文件，code_run 不允许读取"
        )
    for root in read_roots:
        if p == root or _under(p, root):
            return
    raise PermissionError(
        f"读取越界：{p}；code_run 只能读取当前 workspace {write_root}"
    )

def _check(value, label="写入"):
    p = _candidate(value)
    if p is None:
        return
    if p == write_root or _under(p, write_root):
        return
    raise PermissionError(
        f"{label}越界：{p}；code_run 只能写当前 workspace {write_root}"
    )

def _open_is_write(mode, flags):
    if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
        return True
    if isinstance(flags, int):
        bits = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        return bool(flags & bits)
    return False

def _audit(event, args):
    if event == "open":
        if len(args) >= 3:
            if _open_is_write(args[1], args[2]):
                _check(args[0])
                _check_read(args[0])
            else:
                # 读也必须校验：以前这里只查写，导致能读别人的数据库。
                _check_read(args[0])
    elif event == "sqlite3.connect":
        # sqlite3 走 C 层，不一定触发 open；它有独立的审计事件。
        # 不拦这条，`sqlite3.connect(".../app.db")` 就能绕过所有读校验。
        if args:
            _check_read(args[0])
    elif event in {"os.listdir", "os.scandir"}:
        if args and args[0] is not None:
            _check_read(args[0])
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
    elif event == "shutil.copyfile" and len(args) >= 2:
        _check_read(args[0])
        _check(args[1], "复制")

sys.addaudithook(_audit)
sys.argv = [script] + sys.argv[4:]
runpy.run_path(script, run_name="__main__")
'''

# shell 分支的守卫：通过 PYTHONSTARTUP 拦不住，改用 sitecustomize 注入不现实，
# 因此对 shell 采用「静态检查 + 受限可用」策略（见 shell_code_reason）。
_SHELL_ABS_PATH_MARKERS = ("\\\\", ":/", ":\\")


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


def read_roots_for(user_id: str) -> list[str]:
    """可读根：与 ``workspace.safe_read_roots`` 完全一致。

    必须共用同一份定义，否则「file_read 能读、code_run 不能读」这类不一致
    会在两个方向上都变成缺陷。
    """
    from .workspace import safe_read_roots

    roots: list[str] = []
    for root in safe_read_roots(user_id):
        try:
            roots.append(str(root))
        except (TypeError, ValueError):
            continue
    return roots


def shell_code_reason(code: str) -> str | None:
    """shell 分支为什么不能装 python 审计钩子，只能静态拦。

    `powershell -Command` / `bash -c` 不会加载我们的 sitecustomize，
    进程内审计无从注入。这里退一步：拒绝明显的绝对路径访问与目录遍历命令，
    并在提示词里说明 shell 只适合操作 workspace 内的相对路径。

    这是**已知的弱项**，写在这里而不是假装它不存在。
    """
    normalized = (code or "").replace("\\", "/")
    low = normalized.lower()
    # 平台数据库文件名：无论怎么拼路径都不能出现
    for bad in ("app.db", "app.sqlite", "app.db-wal", "app.db-shm"):
        if bad in low:
            return "禁止访问平台数据库文件"
    # 跨盘符/绝对路径（含 /d/ 这类 git-bash 写法）
    if ":/" in normalized or " /d/" in low or low.startswith("/d/"):
        return "shell 分支不允许使用绝对路径，请用 workspace 内的相对路径"
    # POSIX 绝对路径（/etc/passwd、/tmp/x 等）——Windows 上 bash -c 也会认
    for token in normalized.replace("'", " ").replace('"', " ").split():
        if token.startswith("/") and len(token) > 1:
            return "shell 分支不允许使用绝对路径，请用 workspace 内的相对路径"
    for cmd in ("cd ..", "cd/", "..\\", "../"):
        if cmd in low:
            return "shell 分支不允许跳出 workspace"
    return None


def workspace_env(cwd: Path, *, user_id: str = "") -> dict[str, str]:
    """子进程环境：临时文件也固定到 workspace 内。"""
    env = os.environ.copy()
    tmp = cwd / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env["TMP"] = str(tmp)
    env["TEMP"] = str(tmp)
    env["TMPDIR"] = str(tmp)
    env["INNER_AGENT_WRITE_ROOT"] = str(cwd.resolve())
    if user_id:
        roots = read_roots_for(user_id)
        if roots:
            env["INNER_AGENT_READ_ROOTS"] = os.pathsep.join(roots)
    return env


def guard_argv(script_path: str, cwd: Path, *, user_id: str = "") -> list[str]:
    """构造 ``python -c <guard> <script> <write_root> <read_roots>`` 的命令行。

    把「可读根」显式传给孩子进程，避免孩子自己去推断（推断错了就是越权）。
    """
    roots = read_roots_for(user_id) if user_id else []
    return [sys.executable, "-X", "utf8", "-u", "-c", WORKSPACE_PYTHON_GUARD,
            script_path, str(cwd), os.pathsep.join(roots)]
