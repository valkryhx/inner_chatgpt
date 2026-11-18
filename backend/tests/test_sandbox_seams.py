"""接缝改造的单测（设计文档 §8 第 5、6 步）。

锁三件事：
1. **`off` 模式零影响** —— 三个工具的行为与改动前完全一致（最关键的回归基线）；
2. `strict` 模式下走容器（用假 manager，不真起容器）；
3. `auto` 降级时**必须留痕**（铁律 2：绝不静默降级）。
"""
from __future__ import annotations

import asyncio

import pytest

from app import builtin_tools
from app import config as app_config
from app import workspace
from app.sandbox import config as sbx_config
from app.sandbox import manager as sbx_manager
from app.sandbox import policy as sbx_policy
from app.sandbox import pool as sbx_pool
from app.sandbox import runtime as sbx_runtime


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def ws_root(tmp_path, monkeypatch):
    """把两个 workspace 根都指到 tmp，避免碰真实数据（尤其是真实的 D:\\sandbox）。

    ``workspaces_root()`` 读的是 ``workspace`` 模块的全局名，所以这里 patch
    的是 ``workspace`` 而不是 ``app_config`` —— 全仓 20 处既有测试都这么写，
    不要改成 patch config（那会让它们全部失效）。

    ⚠️ 必须同时 patch ``SANDBOX_WORKSPACES_DIR``：白名单用户解析到它，
    只 patch ``WORKSPACES_DIR`` 会让测试用户落到真实 ``D:\\sandbox`` 下，
    与上一轮测试留下的文件串味（曾因此让 read-before-write 门禁误触发）。
    """
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    monkeypatch.setattr(workspace, "SANDBOX_WORKSPACES_DIR", tmp_path / "sandbox")
    monkeypatch.setattr(app_config, "WORKSPACES_DIR", tmp_path / "ws")
    monkeypatch.setattr(app_config, "SANDBOX_WORKSPACES_DIR", tmp_path / "sandbox")
    # 默认白名单为空：测试用户走普通 workspace，保持路径可预测
    monkeypatch.setattr(workspace, "SANDBOX_WORKSPACE_USERS", ())
    return tmp_path


def tools_for(uid: str) -> dict:
    items = builtin_tools.build_builtin_tools(uid, include_memory=False)
    return {t.name: t for t in items}


def call(tool, **args) -> str:
    return run(tool.call(args))


# ---------------- off 模式：零影响 ----------------


def test_off_mode_code_run_uses_in_process(ws_root, monkeypatch):
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "off")
    sbx_config.get_config(reload=True)

    t = tools_for("u_off")
    out = call(t["code_run"], code="print('in-process')")
    assert "in-process" in out
    assert "未隔离" not in out, "off 模式不该出现任何沙箱标记"


def test_off_mode_file_write_uses_in_process(ws_root, monkeypatch):
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "off")
    sbx_config.get_config(reload=True)

    t = tools_for("u_off")
    out = call(t["file_write"], path="a.txt", content="hello")
    assert "已写入" in out
    assert "未隔离" not in out
    assert (ws_root / "ws" / "u_off" / "a.txt").read_text(encoding="utf-8") == "hello"


def test_off_mode_policy_never_selects_sandbox(monkeypatch):
    """最硬的断言：off 时三个工具的分流判定全部为 False。"""
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "off")
    cfg = sbx_config.get_config(reload=True)
    for tool in ("code_run", "file_write", "file_patch"):
        assert sbx_policy.should_use_sandbox(tool, "u_anything", cfg) is False


# ---------------- strict 模式：走容器（假 manager） ----------------


class FakeHandle:
    def __init__(self, handled=True, note=""):
        self.handled = handled
        self.note = note
        self.calls = []


@pytest.fixture()
def fake_manager(monkeypatch):
    """替换 get_manager，记录调用并返回预设结果。"""
    state = {"code": (True, "container-output", ""), "write": (True, "已写入 x"),
             "patch": (True, "已局部修改 x"), "calls": []}

    class FakeMgr:
        async def exec_code(self, uid, code, **kw):
            state["calls"].append(("exec_code", uid, code, kw))
            return state["code"]

        async def write_file(self, uid, path, content, **kw):
            state["calls"].append(("write_file", uid, path, kw))
            return state["write"]

        async def patch_file(self, uid, path, old, new, **kw):
            state["calls"].append(("patch_file", uid, path, kw))
            return state["patch"]

    monkeypatch.setattr(sbx_manager, "get_manager", lambda **_kw: FakeMgr())
    return state


def test_strict_code_run_goes_to_container(ws_root, monkeypatch, fake_manager):
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "strict")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "u_mvp")
    sbx_config.get_config(reload=True)
    monkeypatch.setattr(workspace, "SANDBOX_WORKSPACE_USERS", ("u_mvp",))

    t = tools_for("u_mvp")
    out = call(t["code_run"], code="print(1)")
    assert out == "container-output", "strict 下应直接返回容器结果"
    assert fake_manager["calls"][0][0] == "exec_code"


def test_strict_user_outside_whitelist_stays_in_process(ws_root, monkeypatch, fake_manager):
    """白名单外的用户即使 strict 也不走容器 —— MVP 只影响 2 个人。"""
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "strict")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "u_mvp")
    sbx_config.get_config(reload=True)

    t = tools_for("u_other")
    out = call(t["code_run"], code="print('local')")
    assert "local" in out
    assert fake_manager["calls"] == [], "白名单外用户不该调用沙箱"


def test_strict_file_write_goes_to_container(ws_root, monkeypatch, fake_manager):
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "strict")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "u_mvp")
    sbx_config.get_config(reload=True)
    monkeypatch.setattr(workspace, "SANDBOX_WORKSPACE_USERS", ("u_mvp",))

    t = tools_for("u_mvp")
    out = call(t["file_write"], path="b.txt", content="content")
    assert out == "已写入 x"
    assert fake_manager["calls"][0][0] == "write_file"


# ---------------- auto 降级：必须留痕 ----------------


def test_auto_degrade_marks_result(ws_root, monkeypatch, fake_manager):
    """auto 且容器不可用 -> 进程内执行，但结果必须带「未隔离」标记。"""
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "u_mvp")
    sbx_config.get_config(reload=True)
    monkeypatch.setattr(workspace, "SANDBOX_WORKSPACE_USERS", ("u_mvp",))
    # 模拟容器不可用：exec_code 返回 handled=False + 降级说明
    fake_manager["code"] = (False, "[sandbox] 本次未隔离：模拟原因（当前模式 auto）", "")

    t = tools_for("u_mvp")
    out = call(t["code_run"], code="print('degraded')")
    assert "未隔离" in out, "降级必须留痕（铁律 2）"
    assert "degraded" in out, "降级后仍要真的执行"


def test_auto_degrade_file_write_still_writes(ws_root, monkeypatch, fake_manager):
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "u_mvp")
    sbx_config.get_config(reload=True)
    monkeypatch.setattr(workspace, "SANDBOX_WORKSPACE_USERS", ("u_mvp",))
    fake_manager["write"] = (False, "[sandbox] 本次未隔离：模拟原因（当前模式 auto）")

    t = tools_for("u_mvp")
    out = call(t["file_write"], path="fresh-degraded.txt", content="degraded-write")
    assert "未隔离" in out
    # u_mvp 在沙箱白名单里 -> workspace 根是 sandbox/，降级后的进程内写入也该落在那里
    f = ws_root / "sandbox" / "u_mvp" / "fresh-degraded.txt"
    assert f.exists() and f.read_text(encoding="utf-8") == "degraded-write"


# ---------------- 沙箱抛异常不能打断工具 ----------------


def test_sandbox_exception_falls_back(ws_root, monkeypatch):
    """沙箱层任何异常都必须回落进程内，不能把工具本身弄挂。"""
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "strict")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "u_mvp")
    sbx_config.get_config(reload=True)
    monkeypatch.setattr(workspace, "SANDBOX_WORKSPACE_USERS", ("u_mvp",))

    class BoomMgr:
        async def exec_code(self, *a, **kw):
            raise RuntimeError("沙箱炸了")

    monkeypatch.setattr(sbx_manager, "get_manager", lambda **_kw: BoomMgr())

    t = tools_for("u_mvp")
    out = call(t["code_run"], code="print('survived')")
    assert "survived" in out, "沙箱异常后工具仍要能跑"

# ---------------- 可观测性：分流必须留痕（自测/排查的唯一判据） ----------------


def test_sandbox_path_emits_log(ws_root, monkeypatch, caplog):
    """走沙箱时必须打一条 INFO 日志，写明工具/用户/去向。

    为什么必须有：沙箱生效时**工具结果和用户界面完全看不出来**——
    code_run 的输出一样、file_write 的文案一样。没有日志，
    「到底走没走沙箱」只能靠猜，自测和线上排查都会失去判据。

    这里**不用 fake_manager**：留痕是 SandboxManager 自己的行为，
    换成假 manager 就测不到了。改为真实 manager + 假 pool/runtime。
    """
    import logging

    monkeypatch.setenv("HLZ_SANDBOX_MODE", "strict")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "u_mvp")
    sbx_config.get_config(reload=True)
    monkeypatch.setattr(workspace, "SANDBOX_WORKSPACE_USERS", ("u_mvp",))

    # 假 runtime：exec 直接返回固定结果，不起容器
    class FakeRuntime:
        def exec_python(self, cid, code, *, timeout_s, cwd="/ws", host_workspace=""):
            async def _go():
                return sbx_runtime.ExecResult(0, "container-output", "mk")
            return _go()

    class FakePool:
        runtime = FakeRuntime()

        async def acquire(self, uid, host_ws):
            return sbx_pool.ContainerEntry(
                user_id=uid, container_id="abc123456789", created_at=0, last_used=0,
                in_flight=1, host_workspace=host_ws)

        async def release(self, entry):
            return None

    real_mgr = sbx_manager.SandboxManager(cfg=sbx_config.get_config(), pool=FakePool())
    monkeypatch.setattr(sbx_manager, "get_manager", lambda **_kw: real_mgr)

    with caplog.at_level(logging.INFO, logger="app.sandbox"):
        t = tools_for("u_mvp")
        out = call(t["code_run"], code="print(1)")

    assert out == "container-output"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("沙箱分流" in m and "code_run" in m and "u_mvp" in m for m in msgs), \
        f"走沙箱没有留痕，日志={msgs!r}"


def test_in_process_path_emits_no_sandbox_log(ws_root, monkeypatch, caplog):
    """不白名单的用户不该产生任何沙箱日志（负对照）。"""
    import logging

    monkeypatch.setenv("HLZ_SANDBOX_MODE", "strict")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "u_mvp")
    sbx_config.get_config(reload=True)
    monkeypatch.setattr(workspace, "SANDBOX_WORKSPACE_USERS", ())

    with caplog.at_level(logging.INFO, logger="app.sandbox"):
        t = tools_for("u_other")
        call(t["code_run"], code="print('local')")

    assert not [r for r in caplog.records if "code_run" in r.getMessage()], \
        "白名单外用户不该产生沙箱日志"


def test_off_mode_emits_no_sandbox_log(ws_root, monkeypatch, caplog):
    """off 模式零日志 —— 这是「零影响」的一部分。"""
    import logging

    monkeypatch.setenv("HLZ_SANDBOX_MODE", "off")
    sbx_config.get_config(reload=True)

    with caplog.at_level(logging.INFO, logger="app.sandbox"):
        t = tools_for("u_mvp")
        call(t["code_run"], code="print('off')")

    assert not [r for r in caplog.records if "code_run" in r.getMessage()]


# ---------------- 路径不泄露宿主系统信息 ----------------


def _fake_pool():
    class FakeRuntime:
        def exec_python(self, cid, code, *, timeout_s, cwd="/ws", host_workspace=""):
            async def _go():
                return sbx_runtime.ExecResult(0, "", "mk")
            return _go()

    class FakePool:
        runtime = FakeRuntime()

        async def acquire(self, uid, host_ws):
            return sbx_pool.ContainerEntry(
                user_id=uid, container_id="abc123456789", created_at=0, last_used=0,
                in_flight=1, host_workspace=host_ws)

        async def release(self, entry):
            return None

    return FakePool()


def test_write_result_does_not_leak_host_path(ws_root, monkeypatch):
    """结果文案不能出现宿主绝对路径（D:\ / C:\ / /home/ / /Users/）。

    为什么：宿主路径会在前端暴露「跑在 Windows 上」「目录结构长什么样」，
    这是使用者不需要、也不该感知的实现细节。用 workspace 相对路径就够
    —— 模型只需要知道「相对 workspace 根在哪」，用户也只需要这个。
    """
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "strict")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "u_mvp")
    sbx_config.get_config(reload=True)
    monkeypatch.setattr(workspace, "SANDBOX_WORKSPACE_USERS", ("u_mvp",))

    mgr = sbx_manager.SandboxManager(cfg=sbx_config.get_config(), pool=_fake_pool())
    monkeypatch.setattr(sbx_manager, "get_manager", lambda **_kw: mgr)

    t = tools_for("u_mvp")
    out = call(t["file_write"], path="sub/dir/leak.txt", content="hi")

    for bad in ("D:\\", "C:\\", "/home/", "/Users/", "AppData"):
        assert bad not in out, f"文案泄露了宿主路径（{bad}）：{out!r}"
    assert "sub/dir/leak.txt" in out, f"相对路径要保留：{out!r}"


def test_in_process_write_result_does_not_leak_host_path(ws_root, monkeypatch):
    """进程内路径同样不能泄露宿主绝对路径（两边一致）。"""
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "off")
    sbx_config.get_config(reload=True)

    t = tools_for("u_off")
    out = call(t["file_write"], path="rel/leak.txt", content="hi")

    for bad in ("D:\\", "C:\\", "/home/", "/Users/", "AppData"):
        assert bad not in out, f"文案泄露了宿主路径（{bad}）：{out!r}"
    assert "rel/leak.txt" in out


# ---------------- display_path 本身 ----------------


def test_display_path_relative_inside_workspace(ws_root, monkeypatch):
    from app.workspace import display_path
    base = ws_root / "ws" / "u_dp"
    f = base / "sub" / "a.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x", encoding="utf-8")
    assert display_path("u_dp", f) == "sub/a.py"


def test_display_path_never_absolute_outside_workspace(ws_root):
    """workspace 之外的路径只给文件名 —— 绝不返回绝对路径。"""
    from app.workspace import display_path
    outside = ws_root / "elsewhere" / "secret.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("x", encoding="utf-8")
    got = display_path("u_dp", outside)
    assert got == "secret.txt", f"越界路径泄露了结构：{got!r}"
    for bad in ("D:\\", "C:\\", "AppData", str(ws_root)):
        assert bad not in got
