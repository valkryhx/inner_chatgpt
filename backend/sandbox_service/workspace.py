"""workspace 路径解析（必须与 `app/workspace.py` **完全一致**）。

## 为什么必须一致

app 侧和沙箱服务侧看到的是**同一份 appdata 卷**（实测三实例同 inode），
如果两边算出的路径不同，就会出现「app 的 file_read 读 A，沙箱写 B」的双视图错位。

## 单根（2026-11-19 收敛）

这里**曾经**支持「两个根 + 白名单」：白名单用户落 `<sandbox 根>/<uid>`，
其余落 `<workspaces 根>/<uid>`。已整块删除。

那个分支是 Docker 方案的遗留（沙箱 = 容器，需要 `-v` 挂一个容器专属目录）。
bwrap 用 `--bind <workspace> /ws`，同一路径进出，任意目录都能 bind，
所以双根在 bwrap 下没有任何收益。

而它已经在生产里造成错位。实测（2026-11-19）：

    app     thinks u_c56b8337dd09 -> .../data/workspaces/u_c56b8337dd09
    sandbox thinks u_c56b8337dd09 -> .../data/sandbox/u_c56b8337dd09

成因是 `HLZ_SANDBOX_MVP_USERS` 在两个容器的 compose 里默认值写法不同，
compose 在宿主各自插值。同名的「空」在 `user_in_scope`（空 = 全部可进沙箱）
和 `workspaces_root`（空 = 没人走沙箱根）里含义还相反 —— 这个坑踩过一次。

**现在的不变量**：一个用户，一个根，不受任何沙箱开关影响。
`HLZ_SANDBOX_MVP_USERS` 仍存在，但只被 `app/sandbox/config.py` 用于
「谁有权进沙箱」，**不再影响路径**。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `HLZ_WORKSPACES_DIR` | 所有用户的 workspace 根（app 侧对应常量 `WORKSPACES_DIR`） |

两边必须给**同一个值**，否则仍会错位。`test_single_workspace_root.py`
钉住了这条。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

WORKSPACES_ENV = "HLZ_WORKSPACES_DIR"

DEFAULT_DATA_DIR = Path("/app/backend/data")
DEFAULT_WORKSPACES_DIR = DEFAULT_DATA_DIR / "workspaces"

_SEGMENT_FORBIDDEN = ("/", "\\")


class PathSecurityError(ValueError):
    """路径越界。"""


def reject_unsafe_segment(segment: str) -> None:
    """与 app 侧同名同义——改一边必须改另一边。

    为什么两处都要有：app 侧的 file_read/file_write 仍在进程内跑，
    服务侧的 bwrap bind 也用这个；规则不一致就会出现
    「app 认为合法、服务认为越界」或反之。
    """
    if not segment or segment in (".", ".."):
        raise PathSecurityError("非法路径片段")
    for bad in _SEGMENT_FORBIDDEN:
        if bad in segment:
            raise PathSecurityError("非法路径片段（含分隔符）")


def workspaces_dir() -> Path:
    """所有用户的 workspace 根。"""
    raw = os.environ.get(WORKSPACES_ENV)
    return Path(raw).expanduser() if raw else DEFAULT_WORKSPACES_DIR


def workspaces_root(user_id: str) -> Path:
    """该用户的 workspace **根目录**（不创建）。与 app 侧同名函数一一对应。

    单根：`<workspaces 根>/<uid>`，与任何沙箱开关无关。
    详细理由见模块 docstring。
    """
    reject_unsafe_segment(user_id)
    return workspaces_dir() / user_id


# 兼容旧名（本模块第一版叫 user_workspace）
def user_workspace(user_id: str, *, create: bool = False) -> Path:
    root = workspaces_root(user_id)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_dir_shared(path: Path, *, boundary: Path | None = None) -> None:
    """从 `boundary`（默认该用户 workspace 根）**逐级向下**建好 `path`，
    并让每一层都 `g+w` + setgid。

    ## 为什么必须「先修父级、再建子级」

    第一版是「先 `mkdir(parents=True)` 再向上 chmod」，在**遗留目录**上会失败：

        实测（2026-11-20 真机）：
        .subagents                 755 root:UNKNOWN   ← 旧代码建的，组不是 1000
        .subagents/co_8d8bd184324a 755 root:root
        .subagents/.../writer/trump   ← 要在这里写，但父级不可写

    uid 1000 在 755 root:root 的目录里**连 mkdir 都不允许**，
    所以 `mkdir(parents=True)` 直接抛 PermissionError —— 还没跑到 chmod 就死了。

    正确顺序：从边界往下，**每层先 chmod 到可写，再建下一层**。
    这样遗留目录会被顺手修好（不需要单独的数据修复脚本）。

    ## boundary 的作用

    不能无脑往上 chmod —— 一直往上会改到系统目录。边界是该用户的
    workspace 根（那是我们自己的），再往上不属于本服务的职责。

    Args:
        path: 要确保存在的目录。
        boundary: 停止上溯的位置；None 时用 `workspaces_root` 推不出来，
                  只修正 `path` 本身及其下（不修祖先）—— 调用方应尽量传。
    """
    path = Path(path)
    if boundary is None:
        # 没有边界时退化成"只处理自己"，不碰祖先（保守）
        _mkdir_and_fix(path, None)
        return

    boundary = Path(boundary)
    try:
        rel = path.resolve().relative_to(boundary.resolve())
    except (ValueError, OSError):
        # path 不在 boundary 内：交给调用方的路径校验处理，这里只建自己
        _mkdir_and_fix(path, None)
        return

    # 共享组取**边界自己**的组（部署里是 sandbox-perms chgrp 过的那一层）。
    # 不能取父目录：遗留目录的父级本身可能就不对，会一路传染。
    gid = None
    try:
        gid = boundary.stat().st_gid
    except OSError:
        pass

    # 从 boundary 往下逐级：每一层先确保权限，再建下一层
    current = boundary
    _mkdir_and_fix(current, gid)
    for part in rel.parts:
        current = current / part
        _mkdir_and_fix(current, gid)


def _stat_mode(p: Path) -> tuple[int, int]:
    """返回 `(st_mode, st_gid)`；抽成一层便于测试注入（见 app 侧同名函数）。"""
    st = p.stat()
    return st.st_mode, st.st_gid


def _mkdir_and_fix(p: Path, gid: int | None = None) -> None:
    """建这一层（容错），把它 chgrp 到共享组并 chmod 成 `g+w` + setgid。

    ## 为什么服务侧也要 chgrp（虽然它多半 chgrp 不了）

    uid 1000 只能 chgrp 到自己所属的组、chmod 自己拥有的目录。对
    `755 root:root` 的遗留目录，这两步都会 `Operation not permitted` ——
    实测确认过。所以**真正的修复责任在 app（root）**，
    见 `app/sandbox/manager.py::write_file` 里为什么要在分发前先建目录。

    这里仍写一遍的理由：app 已修好的目录在这层是**幂等**的（一次 stat
    判等就跳过），而万一 app 侧漏了、或将来有别的调用方直连，
    服务侧至少能修自己能修的部分，而不是直接硬失败。

    `mkdir` 失败时**不抛**：可能是并发已建（exist_ok）、也可能是别的进程
    正在建。真正的失败会在后面写文件时暴露成明确的 507，比在这里吞掉
    更难排查 —— 但那属于调用方的错误映射职责，这里只负责尽力而为。
    """
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    chown = getattr(os, "chown", None)
    if gid is not None and chown is not None:
        try:
            _, cur_gid = _stat_mode(p)
            if cur_gid != gid:
                chown(p, -1, gid)          # -1 = 不动属主
        except OSError:
            pass

    try:
        cur_mode, _ = _stat_mode(p)
    except OSError:
        return
    current = cur_mode & 0o7777
    want = current | 0o2000 | 0o020       # setgid + g+w，保留其余位
    if current != want:
        try:
            p.chmod(want)
        except OSError:
            pass


def resolve_in_workspace(user_id: str, rel_path: str) -> Path:
    """把相对路径解析到该用户 workspace 内，越界抛 `PathSecurityError`。

    ## 服务侧必须自己校验，不能只信 app

    app 侧已经用 `safe_workspace_path()` 校验过一次，然后把相对路径发过来。
    但**服务侧不能因此跳过校验**：一旦 app 被攻破（或有别的调用方直连，
    或将来加了别的入口），只信调用方就等于把「任意位置写文件」开放出去。

    这跟 `reject_unsafe_segment` 在两边各有一份是同一个道理 ——
    校验的长度是重复的，但它防的是「一边被绕过」。

    ## 规则

    - 只接受**相对路径**（绝对路径直接拒：app 的契约就是发相对的，
      接受绝对的等于给了一个绕过 workspace 的入口）
    - 解析后必须仍在该用户 workspace 内（挡 `..` 与符号链接）

    Args:
        user_id: 已认证的用户（来自 header，不是请求体）。
        rel_path: 相对 workspace 根的路径，如 `notes/a.md`。

    Raises:
        PathSecurityError: 绝对路径 / 空路径 / 解析后越界。
    """
    raw = (rel_path or "").strip()
    if not raw:
        raise PathSecurityError("path 不能为空")

    candidate = Path(raw)
    if candidate.is_absolute():
        raise PathSecurityError(f"只接受相对路径（收到 {raw!r}）")

    root = workspaces_root(user_id).resolve()
    try:
        target = (root / candidate).resolve()
    except OSError as exc:
        raise PathSecurityError(f"无法解析路径: {raw}") from exc

    if target != root and root not in target.parents:
        raise PathSecurityError(f"路径越界（必须位于 {root} 内）: {raw}")
    return target


def make_resolver():
    """给 `Executor` 用的 resolver：`user_id -> Path`。"""

    def resolve(user_id: str) -> Optional[Path]:
        return workspaces_root(user_id)

    return resolve