"""BwrapExecutor 的行为测试（TDD RED）。

分成两组：

- **纯逻辑组**（不需要 bwrap）：命令构造、参数正确性、错误处理。
  这组在 Windows / 没装 bwrap 的机器上也能跑，是 CI 的主力。
- **真执行组**（需要 bwrap）：标记 `requires_bwrap`，本机跑不了就 skip。

为什么要这么分：本机（Windows + Docker Desktop）宿主没有 bwrap，
但它**在容器里有**。所以纯逻辑组保证改代码不破坏命令构造，
真执行组在容器里跑（见 `docs/probes/20261119-bwrap可行性实测.sh` 的做法）。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

HAS_BWRAP = shutil.which("bwrap") is not None
requires_bwrap = pytest.mark.skipif(not HAS_BWRAP, reason="bwrap 未安装")


@pytest.fixture()
def ws(tmp_path):
    """一个用户 workspace 目录。"""
    d = tmp_path / "u_test"
    d.mkdir()
    return d


@pytest.fixture()
def ex(ws):
    from sandbox_service.executor.bwrap import BwrapExecutor

    return BwrapExecutor(workspace_resolver=lambda uid: ws if uid == "u_test" else None)


# ---------------------------------------------------------------- 纯逻辑组


def test_build_argv_contains_all_mandatory_flags(ex, ws):
    """命令必须包含方案 §8 里那套「缺一个就出事」的参数。

    每一条都是实测踩出来的（见 docs/probes/20261119-bwrap可行性实测.sh）：
    - --unshare-all   不加就 EPERM（容器里无 CAP_SYS_ADMIN）
    - --clearenv      不加 DB_PASSWORD 原样漏进沙箱
    - --cap-drop ALL  不加 chroot 能成功
    - --tmpfs /etc    不加 /etc/hosts 写得进去
    """
    argv = ex._build_argv(ws, "print(1)", "python")

    assert "--unshare-all" in argv
    assert "--clearenv" in argv
    assert "--cap-drop" in argv and "ALL" in argv
    # /etc 必须先占位再叠 fonts（否则 bwrap 自动建的父目录是可写的）
    assert "--tmpfs" in argv
    assert "/etc" in argv
    etc_idx = argv.index("/etc")
    fonts_idx = argv.index("/etc/fonts")
    assert etc_idx < fonts_idx, "--tmpfs /etc 必须在 --ro-bind /etc/fonts 之前"


def test_build_argv_does_not_ro_bind_root(ex, ws):
    """绝不能 `--ro-bind / /`——那会把 app 的 .env 暴露给沙箱。"""
    argv = ex._build_argv(ws, "print(1)", "python")

    # 检查每一对 --ro-bind 的源，不能出现单独的 "/"
    for i, tok in enumerate(argv):
        if tok == "--ro-bind":
            src = argv[i + 1]
            assert src != "/", "--ro-bind / / 会暴露 app 源码与密钥"


def test_build_argv_binds_only_user_workspace(ex, ws):
    """只 bind 自己的目录，不 bind 父目录（跨租户隔离，实测过的坑）。"""
    argv = ex._build_argv(ws, "print(1)", "python")

    # 找到 bind 到 /ws 的那一项
    pairs = [
        (argv[i + 1], argv[i + 2])
        for i, tok in enumerate(argv)
        if tok in ("--bind", "--ro-bind", "--dev-bind")
    ]
    to_ws = [src for src, dst in pairs if dst == "/ws"]
    assert to_ws == [str(ws)], f"应当只 bind 用户目录到 /ws，实际: {to_ws}"


def test_build_argv_uses_clearenv_then_setenv(ex, ws):
    """--clearenv 之后必须显式 setenv PATH/HOME，否则连 python 都找不到。"""
    argv = ex._build_argv(ws, "print(1)", "python")

    setenvs = {}
    for i, tok in enumerate(argv):
        if tok == "--setenv":
            setenvs[argv[i + 1]] = argv[i + 2]

    assert "PATH" in setenvs
    assert "/usr/bin" in setenvs["PATH"] or "/usr/local/bin" in setenvs["PATH"]
    assert "HOME" in setenvs


def test_build_argv_applies_resource_limits(ex, ws):
    """资源限制靠 prlimit（bwrap 自己不做限制）。"""
    argv = ex._build_argv(ws, "print(1)", "python")

    assert "prlimit" in argv
    joined = " ".join(argv)
    assert "--as=" in joined, "缺内存限制"
    assert "--cpu=" in joined, "缺 CPU 限制"
    assert "--fsize=" in joined, "缺文件大小限制"


def test_build_argv_shell_mode(ex, ws):
    """code_type=sh 时用 sh -c，不是 python。"""
    argv = ex._build_argv(ws, "echo hi", "sh")

    joined = " ".join(argv)
    assert "sh" in argv or "/bin/sh" in joined
    assert "python3" not in argv


def test_workspace_resolver_error_is_explicit():
    """resolver 返回 None 时必须报错，不能拿个空目录凑合。"""
    from sandbox_service.executor.bwrap import BwrapExecutor

    ex = BwrapExecutor(workspace_resolver=lambda uid: None)
    with pytest.raises(Exception) as ei:
        asyncio.run(ex.exec("nobody", "print(1)"))
    assert "workspace" in str(ei.value).lower()


def test_health_reports_missing_bwrap(monkeypatch):
    """bwrap 不存在时 health 必须如实报告，不能假装 ok。"""
    from sandbox_service.executor import bwrap as mod

    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    ex = mod.BwrapExecutor(workspace_resolver=lambda uid: Path("/tmp"))
    ok, reason = asyncio.run(ex.health())
    assert ok is False
    assert "bwrap" in reason.lower()


def test_destroy_unknown_user_is_noop(ex):
    """destroy 一个没 start 过的用户不报错（幂等）。"""
    asyncio.run(ex.destroy("never_started"))


# ---------------------------------------------------------------- 真执行组


@requires_bwrap
def test_exec_returns_stdout(ex, ws):
    r = asyncio.run(ex.exec("u_test", "print(1+1)"))
    assert r.exit_code == 0
    assert r.stdout.strip() == "2"


@requires_bwrap
def test_exec_captures_stderr_and_exit_code(ex, ws):
    r = asyncio.run(ex.exec("u_test", "import sys; print('e', file=sys.stderr); sys.exit(3)"))
    assert r.exit_code == 3
    assert "e" in r.stderr


@requires_bwrap
def test_exec_can_write_workspace(ex, ws):
    r = asyncio.run(ex.exec("u_test", "open('/ws/a.txt','w').write('hi')"))
    assert r.exit_code == 0
    assert (ws / "a.txt").read_text() == "hi"


@requires_bwrap
def test_exec_cannot_write_usr(ex):
    r = asyncio.run(ex.exec("u_test", "open('/usr/x','w').write('x')"))
    assert r.exit_code != 0


@requires_bwrap
def test_exec_cannot_reach_network(ex):
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1',443), timeout=2)\n"
        "    print('CONNECTED')\n"
        "except OSError:\n"
        "    print('BLOCKED')\n"
    )
    r = asyncio.run(ex.exec("u_test", code))
    assert "BLOCKED" in r.stdout


@requires_bwrap
def test_exec_times_out(ex):
    r = asyncio.run(ex.exec("u_test", "import time; time.sleep(30)", timeout_s=1))
    assert r.exit_code != 0
    # 超时要能从结果里看出来
    assert "timeout" in (r.stderr + r.stdout).lower() or r.exit_code in (124, 137)