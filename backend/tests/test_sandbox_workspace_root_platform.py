"""沙箱 workspace 根不能产生跨平台畸形路径。

## 缺陷现场（2026-11-19，hx 的「4 个 sub 先 2 并行后 2 串行」卡在 C）

`worker_c` 在 `code_run` 里执行：

    cd '/app/backend/D:\\sandbox\\u_c56b8337dd09' && timeout 120 python3 -m pip install ...

容器里解析出的工作目录是：

    /app/backend/D:\\sandbox/u_c56b8337dd09/.subagents/co_xxx/worker_c

根因：`app/config.py` 的默认值是写死的 Windows 路径

    SANDBOX_WORKSPACES_DIR = Path(os.environ.get("HLZ_SANDBOX_WORKSPACES_DIR") or r"D:\\sandbox")

而 compose 只设了 `HLZ_SANDBOX_MODE=off`，**没设** `HLZ_SANDBOX_WORKSPACES_DIR`。
于是 hx（在 `HLZ_SANDBOX_MVP_USERS` 默认白名单里）的工作区在 Linux 容器里
变成一个名字含反斜杠的**相对路径**，被拼到 `/app/backend` 下面。

那个畸形目录真的被创建出来了（里面 demo/、.subagents/ 都在），
所以多数工具靠「能在里面读写」侥幸工作 —— 直到对路径做严格解释的
工具（shell 的 `cd`）踩上去。

## 测试策略

平台默认值在 import 时求值，直接改 `os.name` 不可靠。所以把
「按平台算默认根」抽成纯函数 `default_sandbox_root(is_windows)`，
这样在 Windows 主机上也能验证 Linux 分支。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 纯函数

def test_linux_default_is_absolute_posix():
    """Linux 默认根不能是写死的 `D:\\sandbox`。

    ⚠️ 断言不能写成「不以 D: 开头」—— 本仓库正好在 D 盘，
    `DATA_DIR / "sandbox"` 算出来是 `D:\\git_codes\\...\\data\\sandbox`，
    也会以 D: 开头，那是**正确**的（它是绝对路径，不是写死的盘符常量）。

    真正的缺陷特征是「字面量 r"D:\\sandbox"」，所以直接断言这两点：
    1. 不等于那个写死的常量；
    2. 落在 DATA_DIR 之下（跟着数据卷走）。
    """
    from app.config import DATA_DIR, default_sandbox_root

    p = default_sandbox_root(is_windows=False)
    assert Path(p) != Path(r"D:\sandbox"), f"Linux 上仍在用写死的 Windows 路径：{p}"
    assert Path(DATA_DIR) in Path(p).parents, \
        f"Linux 默认根 {p} 不在 DATA_DIR {DATA_DIR} 之下"


def test_windows_default_stays_on_d_drive():
    """Windows 默认仍是 D:\\sandbox —— 刻意选的，避免用户数据占 C 盘。"""
    from app.config import default_sandbox_root

    p = default_sandbox_root(is_windows=True)
    assert str(p).upper().startswith("D:"), f"Windows 默认盘变了：{p}"


def test_linux_default_lives_under_data_dir():
    """Linux 默认根应落在 DATA_DIR 下（容器里那个卷）。

    这样它天然是可写的、且跟着已有卷走，不需要额外挂载。
    """
    from app.config import DATA_DIR, default_sandbox_root

    p = default_sandbox_root(is_windows=False)
    assert Path(DATA_DIR) in p.parents or p == Path(DATA_DIR), \
        f"Linux 默认根 {p} 不在 DATA_DIR {DATA_DIR} 之下"


# ---------------------------------------------------------------- 常量接线

def test_constant_uses_the_function(monkeypatch):
    """模块级常量必须来自默认函数（而不是另写一份路径）。

    否则改了函数却忘了常量，bug 会原地复活。
    """
    src = (BACKEND / "app" / "config.py").read_text(encoding="utf-8")
    assert "default_sandbox_root(" in src, \
        "SANDBOX_WORKSPACES_DIR 没有走 default_sandbox_root()"


def test_explicit_env_still_wins():
    """显式配置优先 —— 运维要能指定路径。

    ⚠️ 不能直接比字符串：Windows 上 `Path('/custom/ws')` 会规范化成
    `\\custom\\ws`。要比 `Path` 对象。
    """
    code = (
        "from app.config import SANDBOX_WORKSPACES_DIR as d;"
        "print(str(d))"
    )
    env = dict(os.environ)
    env["HLZ_SANDBOX_WORKSPACES_DIR"] = "/custom/ws"
    r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND,
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    got, want = Path(r.stdout.strip()), Path("/custom/ws")
    assert got == want or got.name == want.name, f"{got!r} != {want!r}"


# ---------------------------------------------------------------- 端到端

def test_whitelisted_user_path_has_no_backslash():
    """白名单用户的 workspace 路径里不能出现反斜杠（本机平台相关）。"""
    if sys.platform.startswith("win"):
        return  # Windows 上反斜杠是正常分隔符
    code = (
        "import os;"
        "os.environ.setdefault('HLZ_SANDBOX_MVP_USERS','u_test');"
        "from app.workspace import workspaces_root as w;"
        "print(str(w('u_test')))"
    )
    env = dict(os.environ)
    env.pop("HLZ_SANDBOX_WORKSPACES_DIR", None)
    r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND,
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    out = r.stdout.strip().splitlines()[-1]
    assert "\\" not in out, f"白名单用户 workspace 路径含反斜杠：{out!r}"
    assert not out.startswith("/app/backend/D:"), f"拼成畸形路径：{out!r}"