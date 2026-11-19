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


def make_resolver():
    """给 `Executor` 用的 resolver：`user_id -> Path`。"""

    def resolve(user_id: str) -> Optional[Path]:
        return workspaces_root(user_id)

    return resolve