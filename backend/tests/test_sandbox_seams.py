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