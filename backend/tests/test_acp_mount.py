"""S8：``ga_*`` 工具只挂到本轮 root runtime，且必须过 preflight 闸。

设计文档 §7.1（只有 root 拿到工具）与 §11.3（预检失败 = 不注入）。
本文件是全离线的：``preflight`` 一律 monkeypatch，不碰真实 GA 安装。
"""
from __future__ import annotations

import asyncio
import sys

from app.acp.manager import AcpSessionManager, AcpUnavailableError
from app.subagent.context import AgentContext
from app.tools import Tool, ToolRegistry

GA_TOOLS = ("ga_spawn", "ga_wait", "ga_result", "ga_send", "ga_stop")


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
