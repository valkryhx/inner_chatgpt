"""通知持久化：跨 generation 的新 host 仍能拿到未读通知。"""
from __future__ import annotations

import asyncio

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
    async def run(self, messages, tools=None, on_delta=None, stop_check=None):
        return ChatResult(content="结果正文")


class Model:
    def __init__(self, client):
        self.client = client
        self.model_name = "fake"


async def _noop(args):
    return "ok"


class Parent:
    def __init__(self):
        self.model = Model(Client())
        self.registry = ToolRegistry([
            Tool(n, "", {"type": "object", "properties": {}}, _noop)
            for n in ("file_read", "code_run", "spawn_agent", "wait_agent",
                      "read_agent_result", "list_agents", "close_agent")
        ])


def _ctx(session="s1", generation="g1"):
    return AgentContext(user_id="u1", session_id=session, message_id="m1",
                        generation_id=generation)


def _host(generation="g1", session="s1"):
    return SubagentHost(_ctx(session, generation), parent_runtime=Parent(), buffer=EventBuffer())


def test_notification_survives_new_generation(isolated):
    async def flow():
        host = _host(generation="g1")
        await host.spawn(task_name="a", message="m")
        await host.wait(targets=["a"], timeout_s=10)
        await asyncio.sleep(0.2)

        nxt = _host(generation="g2")
        await nxt.load_pending_notifications()
        items = nxt.peek_notifications()
        assert len(items) == 1
        assert items[0]["agent"] == "a"

    run(flow())


def test_acked_notification_is_not_reloaded(isolated):
    async def flow():
        host = _host(generation="g1")
        await host.spawn(task_name="a", message="m")
        await host.wait(targets=["a"], timeout_s=10)
        await asyncio.sleep(0.2)
        host.drain_notifications()
        await asyncio.sleep(0.3)

        nxt = _host(generation="g2")
        await nxt.load_pending_notifications()
        assert nxt.peek_notifications() == []

    run(flow())


def test_notifications_are_session_scoped(isolated):
    async def flow():
        host = _host(generation="g1", session="s1")
        await host.spawn(task_name="a", message="m")
        await host.wait(targets=["a"], timeout_s=10)
        await asyncio.sleep(0.2)

        other = _host(generation="g9", session="s2")
        await other.load_pending_notifications()
        assert other.peek_notifications() == []

    run(flow())


def test_drain_does_not_duplicate_within_one_host(isolated):
    async def flow():
        host = _host()
        await host.spawn(task_name="a", message="m")
        await host.wait(targets=["a"], timeout_s=10)
        await asyncio.sleep(0.2)
        assert len(host.drain_notifications()) == 1
        assert len(host.drain_notifications()) == 0

    run(flow())
