"""多智能体在沙箱里的正确性。

## 场景

一个用户跑多智能体任务时，Hlaudz 会派多个子代理（`subagent/runtime.py`）。
每个子代理拿到的 `code_run` / `file_write` 都带自己的 `write_root`：

    write_root = ".subagents/<session_id>/<agent_name>"

于是宿主的实际目录变成：

    D:\\sandbox\\<user_id>\\.subagents\\<session>\\<agent>

但**容器是按 user_id 一个**（`pool._entries` 的 key 是 user_id）。
所以同一用户的多个子代理会共用同一个容器，而它们的 write_root 不同。

## 这里锁的契约

1. 同一用户的多个子代理**共用一个容器**（不重复起容器，省资源）；
2. 但每次 exec 的**挂载点必须与自己的 write_root 对应**，不能串到别人的目录；
3. 并发时不能出现「A 的进程在 B 的挂载点下跑」。

第 2、3 条是本节的核心 —— 如果容器只挂了一个固定的 `/ws`，
那 `write_root` 就失效了，所有子代理都会写到用户根目录，互相覆盖。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app.sandbox import pool as sbx_pool
from app.sandbox import runtime as sbx_runtime
from app.sandbox.config import SandboxConfig


def make_cfg(**kw) -> SandboxConfig:
    base = dict(mode="strict", idle_ttl_s=900, max_containers=10, image="img:test",
                mvp_users=("u_a",))
    base.update(kw)
    return SandboxConfig(**base)


class RecordingRuntime:
    """记录每次 create 用的挂载目录，用来判断有没有重复创建 / 挂错目录。"""

    def __init__(self):
        self.created: list[tuple[str, str]] = []
        self.alive: dict[str, bool] = {}

    def create(self, user_id: str, host_workspace: str) -> str:
        cid = f"cid_{user_id}_{len(self.created)}"
        self.created.append((user_id, host_workspace))
        self.alive[cid] = True
        return cid

    def destroy(self, container_id: str) -> None:
        self.alive[container_id] = False

    def is_alive(self, container_id: str) -> bool:
        return self.alive.get(container_id, False)


def test_same_user_subagents_share_one_container():
    """同一用户的多个子代理只起一个容器 —— 否则 10 个 agent 就是 10 个容器。"""
    rt = RecordingRuntime()
    p = sbx_pool.ContainerPool(cfg=make_cfg(), runtime=rt)

    async def go():
        # 模拟 3 个 agent 各自的 write_root
        roots = [".subagents/sess/a1", ".subagents/sess/a2", ".subagents/sess/a3"]
        entries = [await p.acquire("u_a", f"D:\\sandbox\\u_a\\{r}") for r in roots]
        return entries

    entries = asyncio.run(go())
    assert len(rt.created) == 1, (
        f"同一用户起了 {len(rt.created)} 个容器，应该只起 1 个：{rt.created}")
    # 三个都指向同一个容器
    assert len({e.container_id for e in entries}) == 1


def test_mount_point_must_follow_write_root():
    """容器只有一个，但每次 exec 的挂载点要跟着 write_root 走。

    ⚠️ 这条是关键：如果容器创建时就固定挂死了某个 write_root，
    其他子代理就会写到别人的目录里。
    """
    src = inspect.getsource(sbx_runtime.ContainerRuntime.create)
    # 挂载应该来自参数 host_workspace，而不是某个固定常量
    assert "host_workspace" in src, "create() 没用 host_workspace 参数"

    src_exec = inspect.getsource(sbx_runtime.ContainerRuntime.exec_python)
    assert "host_workspace" in src_exec, (
        "exec_python() 没有 host_workspace 参数 —— "
        "脚本会被写到错误的挂载点下")


def test_exec_python_requires_host_workspace():
    """exec_python 必须显式要 host_workspace（无默认值）。

    为什么：默认值会让调用方忘记传，静默落到错误路径。
    这条锁的是「必须显式传」，不是「不能有 container_workspace」——
    后者有默认值是**故意**的（单 agent 时两者相同，可省略）。
    """
    sig = inspect.signature(sbx_runtime.ContainerRuntime.exec_python)
    p = sig.parameters.get("host_workspace")
    assert p is not None, "exec_python 没有 host_workspace 参数"
    assert p.default is inspect.Parameter.empty, (
        "host_workspace 有默认值，调用方漏传时会静默用错目录")


def test_container_workspace_is_optional_but_preferred():
    """container_workspace 可以有默认值，但 manager 必须传它。

    单 agent 时 host_workspace == container_workspace，省略无害；
    多 agent 时两者不同，省略就会把脚本路径算错。
    所以这里检查的是「manager 的三个方法都传了」。
    """
    import inspect as _i
    from app.sandbox.manager import SandboxManager
    for meth in ("exec_code", "write_file", "patch_file"):
        src = _i.getsource(getattr(SandboxManager, meth))
        assert "container_workspace=mount" in src, (
            f"{meth} 没把 container_workspace 传给 exec_python —— "
            "子代理的脚本会写到容器看不到的位置")


def test_mount_root_is_user_root_not_write_root():
    """容器挂载点必须是**用户根目录**，不能是带 write_root 的子目录。

    这是 2026-11-18 真机复现的 bug：容器按主 agent 的根目录建，
    子代理的脚本写到 .subagents/sess/a1/.hlz_tmp/ 下，
    但容器里找 /ws/.hlz_tmp/，报 `can't open file`。
    """
    from app.sandbox.manager import _mount_root
    from app.workspace import user_workspace

    got = _mount_root("u_x")
    want = str(user_workspace("u_x"))
    assert got == want, f"挂载点应该是用户根目录，实际 {got}"


def test_container_key_is_user_not_session():
    """池的 key 是 user_id 而不是 session —— 这是「每用户一容器」的设计。

    如果改成 session 粒度，多智能体就会起 N 个容器；这里锁住现状，
    改的时候必须是有意识的决定（并同步改本测试）。
    """
    p = sbx_pool.ContainerPool(cfg=make_cfg(), runtime=RecordingRuntime())
    assert hasattr(p, "_entries")
    src = inspect.getsource(sbx_pool.ContainerPool.acquire)
    assert "self._entries.get(user_id)" in src, (
        "acquire 的 key 变了，多智能体的容器复用假设不再成立")

def test_write_file_uses_user_root_as_mount(ws_root=None, monkeypatch=None):
    """行为级：write_file 的 pool.acquire 必须收到**用户根目录**。

    为社么要行为级而不只看源码字符串：字符串检查太容易被绕过
    （把 mount = _mount_root(...) 改成 mount = host_ws 时，
    "container_workspace=mount" 这行还在，字符串测试照样绿）。
    """
    import asyncio as _a
    from app.sandbox import config as _cfg
    from app.sandbox.manager import SandboxManager
    from app.workspace import user_workspace
    import app.workspace as wsmod
    import tempfile, pathlib

    tmp = pathlib.Path(tempfile.mkdtemp())
    old_ws, old_users = wsmod.WORKSPACES_DIR, wsmod.SANDBOX_WORKSPACE_USERS
    wsmod.WORKSPACES_DIR = tmp
    wsmod.SANDBOX_WORKSPACES_DIR = tmp
    wsmod.SANDBOX_WORKSPACE_USERS = ("u_x",)
    try:
        acquired: list[str] = []

        class RT:
            def exec_python(self, cid, code, *, timeout_s, cwd="/ws",
                            host_workspace="", container_workspace=""):
                async def _go():
                    return sbx_runtime.ExecResult(0, "", "mk")
                return _go()

        class P:
            runtime = RT()

            async def acquire(self, uid, host_ws):
                acquired.append(host_ws)
                return sbx_pool.ContainerEntry(
                    user_id=uid, container_id="c1", created_at=0, last_used=0,
                    in_flight=1, host_workspace=host_ws)

            async def release(self, entry):
                return None

        cfg = _cfg.SandboxConfig(mode="strict", mvp_users=("u_x",))
        mgr = SandboxManager(cfg=cfg, pool=P())

        root = str(user_workspace("u_x"))
        # 带 write_root 调用（模拟子代理）
        _a.run(mgr.write_file("u_x", "a.txt", "hi", root=".subagents/s/a1"))

        assert acquired, "没有调用 acquire"
        assert acquired[0] == root, (
            f"挂载点应该是用户根目录 {root}，实际是 {acquired[0]}\n"
            "（用 write_root 当挂载点会让子代理脚本写到容器看不到的位置）")
    finally:
        wsmod.WORKSPACES_DIR = old_ws
        wsmod.SANDBOX_WORKSPACES_DIR = old_ws
        wsmod.SANDBOX_WORKSPACE_USERS = old_users
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_manager_concurrent_agents_use_separate_dirs():
    """走 manager 层的并发测试：多个 agent 的 cwd/产物必须各自隔离。

    为什么要这条（真机测试已覆盖但不够）：真机测试直接调 runtime，
    传什么参数由测试自己决定 —— 如果 manager 层传错了，真机测试照样绿。
    这条强制走 manager，验证它真的把 write_root 换算对了。
    """
    import asyncio as _a
    import pathlib
    import tempfile
    from app.sandbox import config as _cfg
    from app.sandbox.manager import SandboxManager
    import app.workspace as wsmod

    tmp = pathlib.Path(tempfile.mkdtemp())
    old = (wsmod.WORKSPACES_DIR, wsmod.SANDBOX_WORKSPACES_DIR,
           wsmod.SANDBOX_WORKSPACE_USERS)
    wsmod.WORKSPACES_DIR = tmp
    wsmod.SANDBOX_WORKSPACES_DIR = tmp
    wsmod.SANDBOX_WORKSPACE_USERS = ("u_x",)
    try:
        seen: list[dict] = []

        class RT:
            def exec_python(self, cid, code, *, timeout_s, cwd="/ws",
                            host_workspace="", container_workspace=""):
                seen.append({"cwd": cwd, "host": host_workspace,
                             "mount": container_workspace})
                async def _go():
                    return sbx_runtime.ExecResult(0, "ok", "mk")
                return _go()

        class P:
            runtime = RT()

            async def acquire(self, uid, mount):
                return sbx_pool.ContainerEntry(
                    user_id=uid, container_id="c1", created_at=0, last_used=0,
                    in_flight=1, host_workspace=mount)

            async def release(self, entry):
                return None

        cfg = _cfg.SandboxConfig(mode="strict", mvp_users=("u_x",))
        mgr = SandboxManager(cfg=cfg, pool=P())

        async def one(agent: str):
            await mgr.exec_code("u_x", f"print('{agent}')",
                                root=f".subagents/s/{agent}")

        async def _run_all():
            await _a.gather(*[one(a) for a in ("a1", "a2", "a3")])

        _a.run(_run_all())

        assert len(seen) == 3
        root = str(tmp / "u_x")
        for rec, agent in zip(seen, ("a1", "a2", "a3")):
            assert rec["mount"] == root, (
                f"{agent} 的挂载点应是用户根 {root}，实际 {rec['mount']}")
            assert rec["cwd"] == f"/ws/.subagents/s/{agent}", (
                f"{agent} 的 cwd 错了：{rec['cwd']}")
            assert ".subagents" in rec["host"] and agent in rec["host"], (
                f"{agent} 的脚本落盘路径不对：{rec['host']}")
    finally:
        wsmod.WORKSPACES_DIR, wsmod.SANDBOX_WORKSPACES_DIR, \
            wsmod.SANDBOX_WORKSPACE_USERS = old
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
