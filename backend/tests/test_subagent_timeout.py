"""子代理墙钟超时：默认必须能承载长任务，且允许按次覆盖。"""
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


class SlowClient:
    def __init__(self, delay=1.0):
        self.delay = delay

    async def run(self, messages, tools=None, on_delta=None, stop_check=None):
        await asyncio.sleep(self.delay)
        return ChatResult(content="done")


class Model:
    def __init__(self, client):
        self.client = client
        self.model_name = "fake"


async def _noop(args):
    return "ok"


class Parent:
    def __init__(self, client):
        self.model = Model(client)
        self.registry = ToolRegistry([
            Tool(n, "", {"type": "object", "properties": {}}, _noop)
            for n in ("file_read", "code_run", "spawn_agent", "wait_agent",
                      "read_agent_result", "list_agents", "close_agent")
        ])


def _ctx():
    return AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")


def _host(client):
    return SubagentHost(_ctx(), parent_runtime=Parent(client), buffer=EventBuffer())


def test_default_timeout_supports_long_tasks(isolated, monkeypatch):
    monkeypatch.delenv("SUBAGENT_TIMEOUT_S", raising=False)
    host = _host(SlowClient())
    assert host.timeout_s >= 1800


def test_spawn_timeout_override(isolated):
    async def flow():
        host = _host(SlowClient(delay=3.0))
        await host.spawn(task_name="a", message="m", timeout_s=0.3)
        await asyncio.sleep(1.0)
        rows = await sub_store.list_runs("s1", "u1")
        assert rows[0]["status"] == "stopped", rows[0]
        assert "timeout" in (rows[0].get("error") or "")

    run(flow())


def test_spawn_agent_schema_has_timeout_seconds():
    from app.subagent.tools import build_subagent_tools

    tool = {t.name: t for t in build_subagent_tools(None)}["spawn_agent"]
    props = tool.input_schema["properties"]
    assert "timeout_seconds" in props
