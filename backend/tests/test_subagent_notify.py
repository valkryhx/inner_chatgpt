"""子代理完成通知：peek/ack 分离 + fold 注入（TDD 红先）。"""
from __future__ import annotations

import asyncio

import pytest

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
    def __init__(self, content="done"):
        self.content = content

    async def run(self, messages, tools=None, on_delta=None, stop_check=None):
        from app.llm_client import ChatResult

        return ChatResult(content=self.content)


class Model:
    def __init__(self, client):
        self.client = client
        self.model_name = "fake"


class Parent:
    def __init__(self, client):
        self.model = Model(client)
        self.registry = ToolRegistry([
            Tool(n, "", {"type": "object", "properties": {}}, _noop)
            for n in ("file_read", "code_run", "spawn_agent", "wait_agent",
                      "read_agent_result", "list_agents", "close_agent")
        ])


async def _noop(args):
    return "ok"


def _ctx():
    return AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")


def _host():
    return SubagentHost(_ctx(), parent_runtime=Parent(Client("结果正文")), buffer=EventBuffer())


def test_notification_contains_agent_and_preview(isolated):
    async def flow():
        host = _host()
        await host.spawn(task_name="a", message="m")
        await host.wait(targets=["a"], timeout_s=10)
        items = host.peek_notifications()
        assert len(items) == 1
        assert items[0]["agent"] == "a"
        assert items[0]["status"] == "completed"
        assert "结果正文" in items[0]["preview"]

    run(flow())


def test_peek_does_not_consume(isolated):
    async def flow():
        host = _host()
        await host.spawn(task_name="a", message="m")
        await host.wait(targets=["a"], timeout_s=10)
        assert len(host.peek_notifications()) == 1
        assert len(host.peek_notifications()) == 1
        host.ack_notifications([host.peek_notifications()[0]["id"]])
        assert host.peek_notifications() == []

    run(flow())


def test_drain_returns_message_and_acks_once(isolated):
    async def flow():
        host = _host()
        await host.spawn(task_name="a", message="m")
        await host.wait(targets=["a"], timeout_s=10)
        first = host.drain_notifications()
        assert len(first) == 1
        assert first[0]["role"] == "user"
        assert "a" in first[0]["content"]
        assert host.drain_notifications() == []

    run(flow())


def test_notification_bounded_to_five_per_drain(isolated, monkeypatch):
    async def flow():
        monkeypatch.setenv("SUBAGENT_MAX_ACTIVE", "10")  # 避免槽位回收影响计数
        host = _host()
        for i in range(7):
            await host.spawn(task_name=f"n{i}", message="m")
        await host.wait(timeout_s=20)
        await asyncio.sleep(0.3)  # 等各节点的 finally 完成通知入队
        batch = host.drain_notifications()
        assert len(batch) == 1  # 一条消息里最多 5 个子代理
        assert "另有" in batch[0]["content"]
        assert len(host.peek_notifications()) == 2

    run(flow())
