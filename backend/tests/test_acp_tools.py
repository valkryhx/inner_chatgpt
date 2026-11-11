import asyncio
import json
import sys

import pytest

from app.acp.config import load_acp_settings
from app.acp.manager import AcpSessionManager, AcpUnavailableError
from app.acp.tools import build_acp_tools
from app.subagent.context import AgentContext
from app.subagent.events import EventBuffer


class FakeManager:
    def __init__(self):
        self.calls = []

    async def spawn(self, **kw):
        self.calls.append(("spawn", kw))
        return {"agent_id": "ga_x", "status": "running"}

    async def wait(self, agent_id, **kw):
        self.calls.append(("wait", agent_id, kw))
        return {"agents": [], "timed_out": False}

    async def result(self, agent_id, **kw):
        self.calls.append(("result", agent_id, kw))
        return {"agent_id": agent_id, "status": "completed", "result": "ok"}

    async def send(self, agent_id, message):
        self.calls.append(("send", agent_id, message))
        return {"agent_id": agent_id, "status": "running"}

    async def stop(self, agent_id):
        self.calls.append(("stop", agent_id))
        return {"stopped": [agent_id] if agent_id else []}


class BrokenManager(FakeManager):
    async def spawn(self, **kw):
        raise RuntimeError("boom")


class Evil:
    """str() 会炸的返回值：用来验证「结果序列化失败也不能抛」。"""

    def __str__(self):
        raise RuntimeError("no str for you")


class UnserializableManager(FakeManager):
    async def stop(self, agent_id):
        self.calls.append(("stop", agent_id))
        return {"stopped": [Evil()]}


def run(coro):
    return asyncio.run(coro)


def tools_of(mgr):
    return {t.name: t for t in build_acp_tools(mgr)}


def test_exact_tool_names():
    assert set(tools_of(FakeManager())) == {
        "ga_spawn", "ga_wait", "ga_result", "ga_send", "ga_stop"}


def test_spawn_passes_arguments():
    mgr = FakeManager()
    out = run(tools_of(mgr)["ga_spawn"].call(
        {"task": "fix it", "title": "T", "mode": "read_only", "timeout_s": 30}))
    assert "ga_x" in out
    assert mgr.calls[0][1] == {"task": "fix it", "title": "T",
                               "mode": "read_only", "timeout_s": 30.0}


def test_wait_defaults():
    mgr = FakeManager()
    run(tools_of(mgr)["ga_wait"].call({}))
    assert mgr.calls[0] == ("wait", None, {"timeout_s": 60.0, "since_seq": 0})


def test_result_close_flag():
    mgr = FakeManager()
    run(tools_of(mgr)["ga_result"].call({"agent_id": "ga_x", "close": True, "full": True}))
    # 默认上限 50000：与 tool_budget.DEFAULT_MAX_RESULT_CHARS 对齐
    # （原 20000 太小，真机上出现过「读到半截还不知道全文多少」的事故）。
    assert mgr.calls[0] == ("result", "ga_x",
                            {"max_chars": 50000, "full": True, "close": True,
                             "force_close": False})


def test_tool_errors_become_readable_text():
    out = run(tools_of(BrokenManager())["ga_spawn"].call({"task": "x"}))
    assert out.startswith("[工具执行错误]")
    assert "boom" in out


def test_spawn_rejects_null_task():
    mgr = FakeManager()
    out = run(tools_of(mgr)["ga_spawn"].call({"task": None}))
    assert out.startswith("[工具执行错误]")
    assert mgr.calls == []


def test_result_close_string_false_is_false():
    mgr = FakeManager()
    run(tools_of(mgr)["ga_result"].call(
        {"agent_id": "ga_x", "close": "false", "full": "0"}))
    assert mgr.calls[0] == ("result", "ga_x",
                            {"max_chars": 50000, "full": False, "close": False,
                             "force_close": False})


def test_serialization_failure_becomes_readable_text():
    out = run(tools_of(UnserializableManager())["ga_stop"].call({}))
    assert out.startswith("[工具执行错误]")
    assert "no str for you" in out


def test_preflight_fails_fast_for_missing_module(tmp_path):
    ctx = AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")
    cmd = json.dumps([sys.executable, "-m", "definitely_not_a_module_xyz"])
    settings = load_acp_settings({"INNER_ACP_GA_COMMAND": cmd})
    mgr = AcpSessionManager(ctx=ctx, buffer=EventBuffer(), settings=settings)
    with pytest.raises(AcpUnavailableError) as exc:
        mgr.preflight()
    assert "definitely_not_a_module_xyz" in str(exc.value)
