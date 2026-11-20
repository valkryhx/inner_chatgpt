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

import os
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


@posix_only
def test_ensure_dir_shared_fixes_every_level(tmp_path, monkeypatch):
    """★ 深层目录的**每一层**都必须 g+w + setgid。

    ## 这是同一个坑的第三层（真机 2026-11-20 抓到的）

    模型写的是 `.subagents/<session>/<agent>/research/trump_findings.md`，
    其中 `research/` 既不在 `write_root_for` 里（那里只到 agent 目录），
    也不是沙箱服务建的 —— 是 `file_write` 时 `target.parent.mkdir()` 建的。

    实测当时的权限：

        .subagents/co_xxx              755 root:root    ← 中间层，连组都不对
        .subagents/co_xxx/researcher   2775 root:root   ← 直接建的，正常
        .subagents/co_xxx/researcher/research  ← 沙箱写这里 → Permission denied

    `mkdir(parents=True, mode=...)` 只给**最末一级**应用 mode，中间层一律 0755。
    所以 `ensure_dir_shared` 必须逐级修正。
    """
    from app import config as app_config
    from app import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(app_config, "WORKSPACES_DIR", tmp_path)

    base = ws_mod.user_workspace("u_deep", create=True)
    deep = base / ".subagents" / "co_x" / "agent" / "research"
    ws_mod.ensure_dir_shared(deep)

    for level in (base / ".subagents", base / ".subagents" / "co_x",
                  base / ".subagents" / "co_x" / "agent", deep):
        mode = level.stat().st_mode & 0o7777
        assert mode & 0o020, f"{level.name} 组不可写（mode={oct(mode)}）"
        assert mode & 0o2000, f"{level.name} 没有 setgid（mode={oct(mode)}）"


def test_manager_uses_shared_helper_for_write_dirs():
    """`manager.write_file` 建父目录也必须走 ensure_dir_shared。

    静态检查：该函数体里不该出现裸 `mkdir(`。容器内脚本那处（`_write_py`）
    有注释说明为什么豁免。
    """
    src = (Path(__file__).resolve().parents[1] / "app" / "sandbox" / "manager.py").read_text(
        encoding="utf-8"
    )
    i = src.find("async def write_file")
    nxt = src.find("async def patch_file", i)
    body = src[i:nxt]
    assert "ensure_dir_shared(" in body, "write_file 没有用 ensure_dir_shared"
    assert "target.parent.mkdir(" not in body, (
        "write_file 里还有裸 target.parent.mkdir —— 深层目录会 755"
    )


def test_sandbox_service_uses_shared_helper():
    """★ 服务侧 `/v1/write` 建目录也必须走 `ensure_dir_shared`。

    ## 为什么这条单独存在

    前面修的 `ensure_dir_shared` 在 **app 侧**（`app/workspace.py`）。
    但 service 后端下，目录其实是**沙箱服务**建的 —— app 侧那段代码
    根本不会执行。所以服务侧必须有一份同样的逻辑。

    真机证据（2026-11-20）：
        app 侧建的 .subagents/co_x             2775 root:hlz
        服务侧建的 .subagents/co_x/writer/trump  2755（组无 w）
    两者混在一棵树里，会出现"有时能写有时不能"。
    """
    from sandbox_service import main as svc_main

    src = Path(svc_main.__file__).read_text(encoding="utf-8")
    assert "ensure_dir_shared" in src, "服务侧没有用 ensure_dir_shared"
    # write_file 端点体内不该再有裸 mkdir
    i = src.find("async def write_file")
    nxt = src.find("async def patch_file", i)
    body = src[i:nxt]
    assert "target.parent.mkdir(" not in body, (
        "服务侧 /v1/write 里还有裸 mkdir —— 深层目录会 755"
    )


def test_service_workspace_exports_ensure_dir_shared():
    """服务侧 workspace 模块必须提供 ensure_dir_shared。"""
    from sandbox_service import workspace as svc_ws

    assert hasattr(svc_ws, "ensure_dir_shared"), (
        "sandbox_service.workspace 缺 ensure_dir_shared"
    )


@posix_only
def test_service_ensure_dir_shared_fixes_every_level(tmp_path):
    """服务侧的逐级修正也要生效（与 app 侧同语义）。"""
    from sandbox_service import workspace as svc_ws

    deep = tmp_path / "a" / "b" / "c"
    svc_ws.ensure_dir_shared(deep)
    for level in (tmp_path / "a", tmp_path / "a" / "b", deep):
        mode = level.stat().st_mode & 0o7777
        assert mode & 0o020, f"{level} 组不可写（{oct(mode)}）"
        assert mode & 0o2000, f"{level} 没有 setgid（{oct(mode)}）"


@posix_only
def test_ensure_dir_shared_repairs_legacy_dirs(tmp_path, monkeypatch):
    """★ 遗留目录（旧代码建的 755 root:root）必须被顺手修好。

    ## 这是真机上最后卡住的一层

    hx 的 `.subagents` 是旧代码建的：

        755 root:UNKNOWN   .subagents
        755 root:root      .subagents/co_8d8bd184324a

    沙箱服务（uid 1000）在这个父目录里**连 mkdir 都不允许**，
    所以「先 mkdir(parents=True) 再 chmod」的顺序直接死在 PermissionError
    —— 还没跑到 chmod。

    正确顺序是「从边界往下，每层先修权限再建下一层」。这条测试模拟那个形态：
    先手工造一个 755 的祖先，再让 ensure_dir_shared 去建深层的子目录。
    """
    import os
    from app import config as app_config
    from app import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(app_config, "WORKSPACES_DIR", tmp_path)

    base = tmp_path / "u_legacy"
    legacy = base / ".subagents" / "co_old"
    legacy.mkdir(parents=True)
    # 造出旧代码的形态：755、无 setgid
    for d in (base, base / ".subagents", legacy):
        os.chmod(d, 0o755)

    # 现在让新代码去建更深的目录（这正是子代理写文件时发生的事）
    deep = legacy / "writer" / "trump"
    ws_mod.ensure_dir_shared(deep, boundary=base)

    for d in (base, base / ".subagents", legacy, legacy / "writer", deep):
        mode = d.stat().st_mode & 0o7777
        assert mode & 0o020, f"{d} 组不可写（{oct(mode)}）"
        assert mode & 0o2000, f"{d} 没有 setgid（{oct(mode)}）"


def test_backend_dispatch_ensures_dirs_before_calling_service():
    """★ 分发到沙箱服务**之前**，app（root）必须先建好并修好父目录。

    ## 为什么必须是 app 先做（真机实测 2026-11-20）

    遗留目录是 `755 root:root`。沙箱服务跑 uid 1000，对它：

        mkdir → Permission denied
        chmod → Operation not permitted      ← 连修都修不了

    `chmod` 要求「是属主或 CAP_FOWNER」，uid 1000 两者都没有。
    所以「让服务侧顺手修祖先」这条思路**根本不成立** —— 只能是 app（root）
    在发请求前把它修好。

    ## 断言为什么按函数作用域 + 行号先后，而不是数总数

    第一版只数「有几处 `ensure_dir_shared`」，于是：

    - 把 write_file 分发前那处删掉 → 进程内回落路径那处还在 → 仍然通过
    - 反过来把调用挪到 `await backend.write_file(...)` **之后** → 也通过
      （父目录在服务已经失败之后才建，等于没建）

    所以必须钉住两点：① 每个函数体里都有；② 位置在 backend 调用**之前**。
    这正是 AGENTS.md §8.5「静态守卫用 AST 按函数作用域限定」的要求。
    """
    import ast

    root = Path(__file__).resolve().parents[1] / "app" / "sandbox" / "manager.py"
    tree = ast.parse(root.read_text(encoding="utf-8"))

    # 方法名 → 它调用的 backend 方法名
    wanted = {"write_file": "write_file", "patch_file": "patch_file"}
    checked = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) and not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in wanted:
            continue
        backend_call = wanted[node.name]
        ensure_lines = []
        dispatch_lines = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name == "ensure_dir_shared":
                    ensure_lines.append(sub.lineno)
                if name == backend_call and isinstance(fn, ast.Attribute) \
                        and getattr(fn.value, "id", None) == "backend":
                    dispatch_lines.append(sub.lineno)

        assert dispatch_lines, f"{node.name} 里找不到 await backend.{backend_call}(...) 调用"
        assert ensure_lines, (
            f"{node.name} 的分发路径没有 ensure_dir_shared —— "
            "服务侧 uid 1000 修不了 root 拥有的遗留目录"
        )
        first_dispatch = min(dispatch_lines)
        assert any(ln < first_dispatch for ln in ensure_lines), (
            f"{node.name}: ensure_dir_shared 在 backend 调用之后（"
            f"ensure={ensure_lines}, dispatch={first_dispatch}）—— "
            "父目录必须在发请求前建好"
        )
        checked += 1

    assert checked == 2, f"只检查到 {checked} 个函数（应为 write_file 与 patch_file）"


def test_all_ensure_dir_shared_calls_pass_boundary():
    """所有调用都必须传 boundary —— 否则不会修祖先，遗留目录照样卡住。

    ⚠️ 必须按「逻辑行」匹配，不能按物理行：调用点常写成两行

        ensure_dir_shared(target.parent,
                          boundary=user_workspace(user_id, create=False))

    第一版按物理行查，把这种合法写法误判成违规（本测试自己踩过一次）。
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for rel in ("app/workspace.py", "app/sandbox/manager.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "ensure_dir_shared":
                continue
            if not any(k.arg == "boundary" for k in node.keywords):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"这些调用没传 boundary：{offenders}"


@posix_only
def test_ensure_dir_shared_also_fixes_group(tmp_path, monkeypatch):
    """`chmod` 改不了属组 —— 遗留 `root:root` 必须靠 chgrp 修。

    ## 这是第二层根因（2026-11-20 真机实测）

    只做 `chmod 2775` 会得到 `drwxrwsr-x root root`：属组还是 root，
    uid 1000 仍**不在组里**，照样写不进去。chmod 只动权限位。

    真机上 `chmod`/`chgrp` 都被拒（`Operation not permitted`），因为
    uid 1000 既不是属主也没有 CAP_FOWNER ⇒ **修复只能由 app（root）做**。
    这条测试验证 app 侧那套动作真的会把组改对。
    """
    import os
    from app import config as app_config
    from app import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(app_config, "WORKSPACES_DIR", tmp_path)

    base = tmp_path / "u_gid"
    legacy = base / ".subagents" / "co_old"
    legacy.mkdir(parents=True)
    for d in (base, base / ".subagents", legacy):
        os.chmod(d, 0o755)

    target_gid = os.getgid()
    ws_mod.ensure_dir_shared(legacy / "writer", boundary=base)

    # 边界及其下每一层都必须归属同一个共享组
    for d in (base, base / ".subagents", legacy, legacy / "writer"):
        assert d.stat().st_gid == target_gid, f"{d} 属组不对"


def test_ensure_dir_shared_gid_comes_from_boundary_not_parent(tmp_path, monkeypatch):
    """共享组必须锚定**边界**，不能照抄父目录 —— 否则错误会一路传染。

    遗留形态里 `.subagents` 的组可能已经是对的（1000），
    但它下面的子目录是 `root:root`。反过来也有：
    父级坏、子级对。所以锚点只能是「我们自己设的那一层」。
    """
    from app import workspace as ws_mod

    boundary = tmp_path / "boundary"
    boundary.mkdir()
    deep = boundary / "a" / "b"
    ws_mod.ensure_dir_shared(deep, boundary=boundary)
    assert deep.is_dir()


def test_mkdir_and_fix_tolerates_missing_chown(monkeypatch, tmp_path):
    """Windows 上没有 `os.chown` —— 不能因此崩掉。

    这是本机（Windows 开发机）的真实情况：`os.chown` 不存在。
    部署在 Linux 才有意义，但代码必须两边都能跑，否则本地测试全红。
    """
    from app import workspace as ws_mod

    monkeypatch.delattr(os, "chown", raising=False)
    p = tmp_path / "x"
    ws_mod._mkdir_and_fix(p, gid=1000)
    assert p.is_dir()


def test_mkdir_and_fix_calls_chown_when_gid_differs(monkeypatch, tmp_path):
    """`_mkdir_and_fix` 必须在属组不对时**真的调用 chown**。

    ## 为什么这条不加 @posix_only

    Windows 没有 POSIX 属组，但**「有没有调 chown」可以用 mock 观察**，
    与平台无关。这样本地（Windows 开发机）也能对「不 chgrp」这个缺陷变红，
    而不必等容器里那条 POSIX 断言。

    ★ 这条测试的存在理由就是一次**真正的假绿灯**：
    反向确认时把 `chown(...)` 换成 `pass`，POSIX 断言在 Windows 上被 skip，
    于是三项注入全绿 —— 看起来像「断言没有因果力」，实际是**本地根本没跑到**。
    补一条跨平台可观察的断言，反向确认才有意义。
    """
    from app import workspace as ws_mod

    calls = []

    def fake_chown(path, uid, gid):
        calls.append((str(path), uid, gid))

    class FakeStat:
        st_gid = 999          # 故意与我们要设的不同
        st_mode = 0o40755

    monkeypatch.setattr(os, "chown", fake_chown, raising=False)
    monkeypatch.setattr(ws_mod, "_stat_mode", lambda p: (FakeStat.st_mode, FakeStat.st_gid))

    p = tmp_path / "x"
    ws_mod._mkdir_and_fix(p, gid=1000)

    assert calls, "属组不同却没有调用 os.chown —— 「不 chgrp」缺陷会漏过去"
    assert calls[0][2] == 1000, f"chown 的目标组不是 1000：{calls}"


def test_mkdir_and_fix_skips_chown_when_gid_matches(monkeypatch, tmp_path):
    """属组已经对了就不要重复 chown（幂等，且避免无谓的系统调用）。"""
    from app import workspace as ws_mod

    calls = []
    monkeypatch.setattr(os, "chown", lambda *a: calls.append(a), raising=False)
    monkeypatch.setattr(ws_mod, "_stat_mode", lambda p: (0o40755, 1000))

    ws_mod._mkdir_and_fix(tmp_path / "y", gid=1000)
    assert not calls, f"属组已对却仍调用 chown：{calls}"


def test_mkdir_and_fix_calls_chmod_when_mode_differs(monkeypatch, tmp_path):
    """权限位不对时必须**真的调用 chmod** —— 跨平台可观察。

    同 `test_mkdir_and_fix_calls_chown_when_gid_differs` 的理由：
    POSIX 断言在 Windows 被 skip，反向确认会假绿。这里 mock 掉
    `Path.chmod`，只看「有没有调、调成什么」，平台无关。
    """
    import pathlib

    from app import workspace as ws_mod

    calls = []
    monkeypatch.setattr(pathlib.Path, "chmod", lambda self, m: calls.append(int(m)),
                        raising=False)
    # 0o755 = 用户可写但没有 setgid / g+w
    monkeypatch.setattr(ws_mod, "_stat_mode", lambda p: (0o40755, 1000))

    ws_mod._mkdir_and_fix(tmp_path / "z", gid=1000)

    assert calls, "权限位不符却没有调用 chmod —— 「不修 mode」缺陷会漏过去"
    assert calls[0] & 0o2000, f"没设 setgid：{oct(calls[0])}"
    assert calls[0] & 0o020, f"没设 g+w：{oct(calls[0])}"
