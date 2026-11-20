"""两个容器共享 workspace 的权限契约。

## 问题

- app 容器以 **root** 运行，创建 `workspaces/<uid>/` 时属主是 root、组是 root
- 沙箱服务以 **uid 1000** 运行（必须，否则 `RLIMIT_NPROC` 失效、fork 炸弹拦不住）
- 结果：沙箱服务写不进 app 创建的目录

实测（2026-11-20，真机）：

    PermissionError: [Errno 13] Permission denied:
      '/app/backend/data/workspaces/u_write_probe/d26cf8f9.txt.hlz-tmp'

`sandbox-perms` 只在**启动时跑一次** `chown -R`，而新用户目录是之后由
app 的 root 建的 —— 一次性的 chown 追不上。

## 方案：共享组 + setgid 位

```
创建组 hlz（gid 1000）
app 容器      → group_add: [1000]        （root 用户附带这个组的权限）
沙箱容器      → 已经是 uid 1000 = 组主
workspaces/   → 2775 (setgid) + chgrp 1000
```

setgid 位（`2` 开头）让**新建子目录与文件自动继承父目录的组**，
所以 app 之后建的任何用户目录都自动属于 `hlz` 组，沙箱服务可写。
这一次性设置就够了，不需要每次建目录都 chown。

## 本文件钉什么

配置层面的断言（Dockerfile / compose）。真正的写权限靠真机 e2e 验证
（`docs/probes/20261120-共享组权限探针.py`）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
COMPOSE = BACKEND / "deploy" / "docker-compose.yml"
SANDBOX_DOCKERFILE = BACKEND / "sandbox_service" / "Dockerfile"


def live_lines(path: Path) -> list[str]:
    """非注释、非空行（注释里当然会提到配置样例，不能误判）。"""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


# ---------------------------------------------------------------- 组


def test_sandbox_image_creates_shared_group():
    """沙箱镜像里建 `hlz` 组，gid 固定 1000。

    为什么 gid 必须固定：app 侧 `group_add: [1000]` 是按**数字**加的
    （容器之间不共享 /etc/group），两边数字不一致就等于没加。
    """
    src = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"groupadd\s+.*--gid\s+1000.*\bhlz\b", src) or \
           re.search(r"groupadd\s+.*-g\s+1000.*\bhlz\b", src), (
        "沙箱镜像没有创建 gid 1000 的 hlz 组"
    )


def test_sandbox_user_joins_shared_group():
    """沙箱用户的主组或附加组必须含 1000（它是组主，天然满足；
    这里钉住 useradd 不会把主组设成别的 gid）。"""
    src = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"useradd\s+.*(?:--gid|-g)\s+(?:1000|hlz)", src) or \
           re.search(r"usermod\s+.*(?:--gid|-g)\s+(?:1000|hlz)", src), (
        "沙箱用户的组不是 gid 1000 —— 它写不进 hlz 组拥有的目录"
    )


def test_app_service_adds_shared_group():
    """app 服务必须 `group_add` 组 1000。

    app 跑 root，root 能写任何文件 —— 但**新建的文件**属主是 root、组是 root。
    加上 group_add 后新建文件属组变成 hlz，沙箱服务才有写权限。
    """
    text = COMPOSE.read_text(encoding="utf-8")
    live = "\n".join(live_lines(COMPOSE))
    assert "group_add" in live, (
        "compose 里 app 服务没有 group_add —— 它新建的文件沙箱写不了"
    )
    assert re.search(r"group_add:\s*\n\s*-\s*['\"]?1000", live), (
        "group_add 里没有 1000（组号）"
    )


# ---------------------------------------------------------------- setgid


def test_perms_sets_setgid_bit():
    """`sandbox-perms` 必须给目录设 setgid 位（2 开头的 mode）。

    `chmod 750` 没有 setgid，app 之后新建的子目录属组仍是 root。
    `chmod 2775` 才能让新建项继承 hlz 组。
    """
    text = COMPOSE.read_text(encoding="utf-8")
    live = "\n".join(live_lines(COMPOSE))
    modes = re.findall(r"chmod\s+(\d{4})\s", live)
    assert modes, "sandbox-perms 里找不到四位 chmod（需要设 setgid）"
    assert any(m.startswith("2") for m in modes), (
        f"chmod 没有 setgid 位（找到 {modes}）—— 新建子目录不会继承组"
    )


def test_perms_chgrp_to_shared_group():
    """目录的属组必须是 1000（hlz），不能只 chown。"""
    text = COMPOSE.read_text(encoding="utf-8")
    live = "\n".join(live_lines(COMPOSE))
    assert re.search(r"chgrp\s+(-R\s+)?1000", live) or \
           re.search(r"chown\s+-R\s+\S*:1000", live), (
        "sandbox-perms 没有把目录属组改成 1000"
    )


def test_perms_is_idempotent_compatible():
    """`sandbox-perms` 用 `set -e`，且命令可重复执行（容器会重启）。"""
    text = COMPOSE.read_text(encoding="utf-8")
    live = "\n".join(live_lines(COMPOSE))
    assert "set -e" in live, "sandbox-perms 没有 set -e，失败会被静默吞掉"
    assert "mkdir -p" in live, "必须用 mkdir -p（重复执行不能报错）"

# ---------------------------------------------------------------- 建目录的 mode


# POSIX 权限位只在类 Unix 上有意义。Windows 的 st_mode 是 FAT/NTFS 的
# 属性位（目录恒 16895 = 0o40777），setgid / g+w 在那里不存在，
# 断言必然假红。真正的验证在容器里做（docs/probes/20261120-共享组权限探针.py）。
posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows 没有 POSIX 权限位（setgid / g+w）"
)


@posix_only
def test_user_workspace_is_group_writable(tmp_path, monkeypatch):
    """★ app 建用户目录时必须 `g+w` + setgid。

    ## 为什么顶层目录设了还不够

    `sandbox-perms` 只能给 `/data/workspaces` **顶层**设 2775。
    用户目录 `workspaces/<uid>/` 是之后由 app 建的，默认 umask 给的是 0755：

        drwxr-sr-x 2 root hlz     ← 组有 r-x 但**没有 w**

    实测症状（2026-11-20 真机）：

        touch: cannot touch '.../u_perm_check/test.tmp': Permission denied

    沙箱服务以 uid 1000（= hlz 组）运行，是这个目录的**组**成员，
    但组没有写权限 —— 所以 setgid 只解决了「组继承」，没解决「组可写」。

    ## 修法

    建目录时显式 `mode=0o2775`（setgid + g+w），不依赖 umask。
    必须显式给：umask 在不同部署里可能不同，靠它等于靠运气。
    """
    from app import config as app_config
    from app import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(app_config, "WORKSPACES_DIR", tmp_path)

    root = ws_mod.user_workspace("u_perm_test", create=True)
    mode = root.stat().st_mode & 0o7777

    assert mode & 0o020, f"组没有写权限（mode={oct(mode)}）—— 沙箱服务写不进去"
    assert mode & 0o2000, f"没有 setgid 位（mode={oct(mode)}）—— 子目录不会继承组"


@posix_only
def test_nested_dirs_inherit_group(tmp_path, monkeypatch):
    """setgid 生效：用户目录下新建的子目录自动继承组与 setgid。"""
    from app import config as app_config
    from app import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(app_config, "WORKSPACES_DIR", tmp_path)

    root = ws_mod.user_workspace("u_nested", create=True)
    sub = root / "sub" / "deeper"
    sub.mkdir(parents=True)          # 用默认 mkdir，靠父目录 setgid 继承

    assert sub.stat().st_mode & 0o2000, (
        "子目录没有继承 setgid —— 父目录的 setgid 没设上"
    )


@posix_only
def test_subagent_write_root_is_group_writable(tmp_path, monkeypatch):
    """★ 子代理的 write_root 目录也必须 g+w + setgid。

    ## 这是同一个坑的第二层

    顶层 `workspaces/` 靠 `sandbox-perms` 设了 2775，用户目录靠
    `user_workspace` 设了 2775 —— 但 `.subagents/<session>/<agent>`
    是**运行时**由 `write_root_for` 新建的，`sandbox-perms` 追不上。

    实测（2026-11-20 真机）：子代理 file_write 报 507 / PermissionError，
    目录是 `drwxr-sr-x`（组有 r-x、没 w）。

    Agent Team 里每个子代理都有自己的 write_root，所以这条不修，
    子代理的产物永远写不出来 —— 这正是「任务无人认领」的物理原因之一。
    """
    from app import config as app_config
    from app import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(app_config, "WORKSPACES_DIR", tmp_path)

    target = ws_mod.write_root_for("u_sub", ".subagents/s1/worker_a")
    mode = target.stat().st_mode & 0o7777

    assert mode & 0o020, f"子代理目录组不可写（mode={oct(mode)}）"
    assert mode & 0o2000, f"子代理目录没有 setgid（mode={oct(mode)}）"


def test_user_workspace_and_write_root_use_same_helper():
    # 静态检查（读源码），不需要 POSIX 权限位，所以不加 @posix_only

    """两处建目录必须走同一个辅助函数（防止将来又有人裸 mkdir）。"""
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parents[1] / "app" / "workspace.py").read_text(
        encoding="utf-8"
    )
    # user_workspace 与 write_root_for 内都不该再出现裸 mkdir
    for fn in ("def user_workspace", "def write_root_for"):
        i = src.find(fn)
        assert i > 0, f"找不到 {fn}"
        nxt = src.find("\ndef ", i + 1)
        body = src[i:nxt if nxt > 0 else len(src)]
        assert "mkdir(" not in body, (
            f"{fn} 里还有裸 mkdir —— 应该用 ensure_dir_shared"
        )
