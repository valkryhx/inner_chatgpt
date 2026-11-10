"""子代理生命周期：关闭竞态、异常终态、等待回执（TDD 红先）。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.llm_client import ChatResult
from app.tools import Tool, ToolRegistry
from app.subagent import store as sub_store
from app.subagent.context import AgentContext
from app.subagent.events import EventBuffer
from app.subagent.host import SubagentHost


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sub_store, "DB_PATH", tmp_path / "sub.db")
    run(sub_store.init_tables())
    from app import workspace
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    return tmp_path


class Client:
    def __init__(self, *, delay=0.0, boom=False, content="done"):
        self.delay = delay
        self.boom = boom
        self.content = content

    async def run(self, messages, tools=None, on_delta=None, stop_check=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.boom:
            raise RuntimeError("model exploded")
        return ChatResult(content=self.content)


class Model:
    def __init__(self, client):
        self.client = client
        self.model_name = "fake"


def _registry():
    async def noop(args):
        return "ok"

    def mk(n):
        return Tool(n, "", {"type": "object", "properties": {}}, noop)
    names = ["file_read", "file_write", "code_run", "load_skill",
             "spawn_agent", "wait_agent", "read_agent_result", "list_agents", "close_agent"]
    return ToolRegistry([mk(n) for n in names])


class Parent:
    def __init__(self, client):
        self.model = Model(client)
        self.registry = _registry()


def _ctx():
    return AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")


def _host(client, buf=None):
    return SubagentHost(_ctx(), parent_runtime=Parent(client), buffer=buf or EventBuffer())


def test_close_after_complete_keeps_completed(isolated):
    async def flow():
        host = _host(Client(content="ok"))
        await host.spawn(task_name="a", message="m")
        await host.wait(targets=["a"], timeout_s=10)
        await host.close("a")
        rows = await sub_store.list_runs("s1", "u1")
        assert rows[0]["status"] == "completed", rows[0]

    run(flow())


def test_close_while_running_marks_stopped_not_completed(isolated):
    async def flow():
        host = _host(Client(delay=1.0, content="late"))
        await host.spawn(task_name="slow", message="m")
        await asyncio.sleep(0.05)
        await host.close("slow")
        await asyncio.sleep(1.2)  # 给被取消的 drive 收尾时间
        rows = await sub_store.list_runs("s1", "u1")
        assert rows[0]["status"] in ("stopped", "cancelled", "errored"), rows[0]
        assert rows[0]["status"] != "completed"

    run(flow())


def test_drive_exception_sets_errored(isolated):
    async def flow():
        host = _host(Client(boom=True))
        await host.spawn(task_name="bad", message="m")
        waited = await host.wait(targets=["bad"], timeout_s=10)
        rows = await sub_store.list_runs("s1", "u1")
        assert rows[0]["status"] == "errored", rows[0]
        assert waited["timed_out"] is False

    run(flow())


def test_wait_timeout_returns_actionable_receipt(isolated):
    async def flow():
        host = _host(Client(delay=1.0))
        await host.spawn(task_name="late", message="m")
        waited = await host.wait(targets=["late"], timeout_s=0.05)
        assert waited["timed_out"] is True
        assert "late" in waited["remaining_targets"]
        assert "recommended_next_action" in waited

    run(flow())


def test_reuse_after_close_gets_suffix(isolated):
    async def flow():
        host = _host(Client(content="ok"))
        await host.spawn(task_name="probe", message="m")
        await host.wait(targets=["probe"], timeout_s=10)
        await host.close("probe")
        second = await host.spawn(task_name="probe", message="m2")
        assert second["agent_id"] == "sub:probe_2", second
        await host.wait(targets=["probe_2"], timeout_s=10)

    run(flow())


def test_reuse_while_running_is_still_rejected(isolated):
    async def flow():
        host = _host(Client(delay=0.5))
        await host.spawn(task_name="busy", message="m")
        with pytest.raises(Exception) as exc:
            await host.spawn(task_name="busy", message="m2")
        assert "仍在运行" in str(exc.value)
        await host.close("busy")

    run(flow())


def test_close_running_publishes_stopping_status(isolated):
    """关闭中的成员必须让前端看到 stopping，否则面板一直显示「运行中」。"""

    async def flow():
        buf = EventBuffer()
        host = _host(Client(delay=1.0, content="late"), buf=buf)
        await host.spawn(task_name="slow", message="m")
        await asyncio.sleep(0.05)
        await host.close("slow")
        seen = []
        while not buf._q.empty():
            seen.append(buf._q.get_nowait())
        statuses = [(e.get("type"), e.get("status")) for e in seen
                    if e.get("type") == "subagent_status"]
        assert ("subagent_status", "stopping") in statuses, statuses

    run(flow())


def test_close_completed_agent_does_not_publish_stopping(isolated):
    """已经结束的成员不该再报 stopping。"""

    async def flow():
        buf = EventBuffer()
        host = _host(Client(content="ok"), buf=buf)
        await host.spawn(task_name="fast", message="m")
        await host.wait(targets=["fast"], timeout_s=10)
        while not buf._q.empty():
            buf._q.get_nowait()
        await host.close("fast")
        seen = []
        while not buf._q.empty():
            seen.append(buf._q.get_nowait())
        assert not [e for e in seen if e.get("type") == "subagent_status"], seen

    run(flow())
