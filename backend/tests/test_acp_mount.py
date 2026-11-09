"""S8：``ga_*`` 工具只挂到本轮 root runtime，且必须过 preflight 闸。

设计文档 §7.1（只有 root 拿到工具）与 §11.3（预检失败 = 不注入）。
本文件是全离线的：``preflight`` 一律 monkeypatch，不碰真实 GA 安装。
"""
from __future__ import annotations

import asyncio
import logging
import sys

import pytest

from app.acp.config import load_acp_settings
from app.acp.manager import AcpSessionManager, AcpUnavailableError
from app.subagent.context import AgentContext
from app.subagent.events import EventBuffer
from app.tools import Tool, ToolRegistry

GA_TOOLS = ("ga_spawn", "ga_wait", "ga_result", "ga_send", "ga_stop")


@pytest.fixture(autouse=True)
def _clean_preflight_cache():
    """预检缓存是模块级状态；每个用例前后都清空，避免互相污染。"""
    from app.acp import manager as manager_mod

    manager_mod.reset_preflight_cache()
    yield
    manager_mod.reset_preflight_cache()


def run(coro):
    return asyncio.run(coro)


def mk(n):
    async def call(args):
        return "ok"
    return Tool(n, "", {"type": "object", "properties": {}}, call)


class FakeModel:
    model_name = "fake"
    client = object()


class FakeBaseLoop:
    system_prompt = "SYS"


class FakeBase:
    model = FakeModel()
    loop = FakeBaseLoop()
    store = object()
    registry = ToolRegistry([mk("file_read")])


def build(tmp_path, monkeypatch):
    from app import workspace
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    from app.subagent import store as sub_store
    monkeypatch.setattr(sub_store, "DB_PATH", tmp_path / "s.db")
    run(sub_store.init_tables())
    from app.subagent.session import build_turn_runtime
    ctx = AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")
    return run(build_turn_runtime(FakeBase(), ctx))


def _enable(monkeypatch):
    """只靠 ``INNER_ACP_GA_PYTHON`` 打开开关（真实安装不参与，preflight 另行替换）。"""
    monkeypatch.delenv("INNER_ACP_GA_COMMAND", raising=False)
    monkeypatch.setenv("INNER_ACP_GA_PYTHON", sys.executable)


def _stub_preflight(monkeypatch, path="/fake/frontends/acp/bridge.py"):
    """替换同步预检：成功时返回假 bridge 路径，并记录被调用次数。"""
    calls = []

    def _preflight(self):
        calls.append(self)
        return path

    monkeypatch.setattr(AcpSessionManager, "preflight", _preflight)
    return calls


def test_no_ga_tools_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("INNER_ACP_GA_COMMAND", raising=False)
    monkeypatch.delenv("INNER_ACP_GA_PYTHON", raising=False)
    turn = build(tmp_path, monkeypatch)
    names = set(turn.loop.registry.names())
    assert turn.acp is None
    assert not [n for n in names if n.startswith("ga_")]
    # 关闭 ACP 不影响既有能力
    assert "file_read" in names
    assert "spawn_agent" in names


def test_ga_tools_injected_when_enabled(tmp_path, monkeypatch):
    _enable(monkeypatch)
    calls = _stub_preflight(monkeypatch)
    turn = build(tmp_path, monkeypatch)
    names = set(turn.loop.registry.names())
    assert turn.acp is not None
    assert isinstance(turn.acp, AcpSessionManager)
    assert len(calls) == 1, "预检是准入闸，本轮装配必须真的跑过一次"
    for n in GA_TOOLS:
        assert n in names, n
    # 现有能力不受影响（additive）
    assert "file_read" in names
    assert "spawn_agent" in names


def test_preflight_failure_drops_ga_tools(tmp_path, monkeypatch):
    _enable(monkeypatch)

    def _boom(self):
        raise AcpUnavailableError("bridge 不存在")

    monkeypatch.setattr(AcpSessionManager, "preflight", _boom)
    turn = build(tmp_path, monkeypatch)
    names = set(turn.loop.registry.names())
    assert turn.acp is None
    assert not [n for n in names if n.startswith("ga_")]
    # 失败只降级 GA，本轮其余工具照常装配
    assert "file_read" in names
    assert "spawn_agent" in names


# ---------- 复审 R14 补的两条 ----------
def test_preflight_none_injects_but_warns_the_gate_was_skipped(tmp_path, monkeypatch, caplog):
    """自定义命令没有可探测模块：preflight 返回 None。

    §11.3 的「不注入」只对**能判定为不可用**的情况成立；任意命令无法在不启动 GA
    的前提下探测，因此这里显式选择「照常注入 + 明确告警」，绝不允许静默放过。
    """
    _enable(monkeypatch)
    monkeypatch.setattr(AcpSessionManager, "preflight", lambda self: None)
    with caplog.at_level(logging.WARNING, logger="app.subagent.session"):
        turn = build(tmp_path, monkeypatch)
    names = set(turn.loop.registry.names())
    assert turn.acp is not None
    for n in GA_TOOLS:
        assert n in names, n
    msgs = [r.getMessage() for r in caplog.records if r.name == "app.subagent.session"]
    hits = [m for m in msgs if "跳过" in m]
    assert hits, f"必须显式告警，实际日志：{msgs}"
    assert sys.executable in hits[0], "告警要带可执行文件（命令行首项）"
    assert "-m" not in hits[0] or "frontends" not in hits[0], "不要把整条命令漏进日志"


def test_preflight_failure_is_negatively_cached(monkeypatch):
    """坏安装不能每轮都 spawn 一次解释器：失败结果按命令行短 TTL 缓存。"""
    from app.acp import manager as manager_mod

    calls = []

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "ModuleNotFoundError: No module named 'frontends'"

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(manager_mod.subprocess, "run", _fake_run)

    ctx = AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")
    settings = load_acp_settings({"INNER_ACP_GA_PYTHON": sys.executable})
    mgr = AcpSessionManager(ctx=ctx, buffer=EventBuffer(), settings=settings)

    for _ in range(2):
        with pytest.raises(AcpUnavailableError):
            mgr.preflight()
    assert len(calls) == 1, "TTL 内必须复用失败缓存，不再跑探测子进程"

    # TTL 过期后必须允许重试（装好依赖无需重启进程）
    monkeypatch.setattr(manager_mod, "_PREFLIGHT_FAIL_TTL_S", 0.0)
    with pytest.raises(AcpUnavailableError):
        mgr.preflight()
    assert len(calls) == 2
