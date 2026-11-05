"""S3: SubagentHost 全链路（假 LLM，无网络）。"""
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
    # isolate workspaces so child write roots land in tmp
    from app import workspace
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    return tmp_path


class FakeClient:
    """按脚本回答：第一次返回 tool_calls（调 file_read），第二次返回文本。"""

    def __init__(self, scripted: list[ChatResult]):
        self.scripted = list(scripted)
        self.calls = 0

    async def run(self, messages, tools=None, on_delta=None, stop_check=None):
        self.calls += 1
        if on_delta and self.scripted:
            pass
        if not self.scripted:
            return ChatResult(content="default done")
        return self.scripted.pop(0)


class FakeModel:
    def __init__(self, client):
        self.client = client
        self.model_name = "fake"


class FakeParentRuntime:
    def __init__(self, client):
        self.model = FakeModel(client)
        self.registry = _parent_registry()


def _parent_registry():
    async def noop(args):
        return "ok"

    def mk(n):
        return Tool(n, "", {"type": "object", "properties": {}}, noop)
    names = ["file_read", "file_write", "file_patch", "list_files", "code_run", "load_skill",
             "spawn_agent", "wait_agent", "read_agent_result", "list_agents", "close_agent"]
    return ToolRegistry([mk(n) for n in names])


def _ctx():
    return AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")


def test_spawn_wait_read_close(isolated):
    async def flow():
        client = FakeClient([
            ChatResult(content="", tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "file_read", "arguments": json.dumps({"path": "x.txt"})},
            }]),
            ChatResult(content="扫描完成：共 3 个函数。"),
        ])
        buf = EventBuffer()
        host = SubagentHost(_ctx(), parent_runtime=FakeParentRuntime(client), buffer=buf)

        out = await host.spawn(task_name="scan", message="看看 x.txt", role="explore")
        assert out["status"] == "running"
        assert out["agent_id"] == "sub:scan"

        waited = await host.wait(targets=["scan"], condition="all_terminal", timeout_s=10)
        assert waited["timed_out"] is False
        assert waited["remaining_targets"] == []
        assert "scan" in waited["result_refs"]
        assert waited["recommended_next_action"] == "read_agent_result"

        res = await host.read_result("scan")
        assert res["ok"] is True
        assert "3 个函数" in res["result"]
        assert res["turns"] >= 1

        closed = await host.close("scan")
        assert closed["ok"] is True
        assert host.registry.get("scan") is None

        # DB 有记录，且 status 在 close 前是 completed
        rows = await sub_store.list_runs("s1", "u1")
        assert len(rows) == 1 and rows[0]["name"] == "scan"

        # 事件扇出：至少 subagent_start / subagent_end
        types = []
        buf.close()
        async for ev in buf.stream():
            types.append(ev["type"])
        assert "subagent_start" in types
        assert "subagent_end" in types

    run(flow())


def test_child_registry_has_no_orchestration_tools(isolated):
    async def flow():
        client = FakeClient([ChatResult(content="done")])
        buf = EventBuffer()
        host = SubagentHost(_ctx(), parent_runtime=FakeParentRuntime(client), buffer=buf)

        captured = {}

        # 直接构造 runtime 检查 registry
        from app.subagent.runtime import SubagentRuntime, SubagentSpec
        node_ctx = _ctx().child("probe")
        spec = SubagentSpec(name="probe", role="general", message="x")
        rt = SubagentRuntime(node_ctx, parent_runtime=FakeParentRuntime(client), spec=spec)
        await rt.build()
        captured["names"] = rt.registry.names()

        for banned in ("spawn_agent", "wait_agent", "list_agents", "close_agent", "read_agent_result"):
            assert banned not in captured["names"], banned
        assert "file_read" in captured["names"]

    run(flow())


def test_wait_timeout_does_not_kill_child(isolated):
    async def flow():
        class SlowClient:
            async def run(self, messages, tools=None, on_delta=None, stop_check=None):
                await asyncio.sleep(1.0)
                return ChatResult(content="done later")

        buf = EventBuffer()
        host = SubagentHost(_ctx(), parent_runtime=FakeParentRuntime(SlowClient()), buffer=buf)
        await host.spawn(task_name="slow", message="慢慢来")
        res = await host.wait(targets=["slow"], condition="all_terminal", timeout_s=0.2)
        assert res["timed_out"] is True
        assert "slow" in res["remaining_targets"]
        # 子代理还在跑
        node = host.registry.get("slow")
        assert node is not None and not node.is_terminal
        res2 = await host.wait(targets=["slow"], condition="all_terminal", timeout_s=5)
        assert res2["timed_out"] is False
        await host.close_all()

    run(flow())


def test_stop_all_stops_children(isolated):
    async def flow():
        class ForeverClient:
            async def run(self, messages, tools=None, on_delta=None, stop_check=None):
                for _ in range(100):
                    if stop_check and stop_check():
                        return ChatResult(content="stopped")
                    await asyncio.sleep(0.05)
                return ChatResult(content="done")

        buf = EventBuffer()
        host = SubagentHost(_ctx(), parent_runtime=FakeParentRuntime(ForeverClient()), buffer=buf)
        await host.spawn(task_name="loop1", message="x")
        await host.spawn(task_name="loop2", message="y")
        await asyncio.sleep(0.15)
        await host.stop_all()
        await asyncio.sleep(0.3)
        statuses = {n["name"]: n["status"] for n in host.list_agents(include_closed=True)}
        assert statuses.get("loop1") in ("stopped", "completed")
        await host.close_all()

    run(flow())


def test_slot_limit_is_per_host(isolated):
    async def flow():
        class SlowClient:
            async def run(self, messages, tools=None, on_delta=None, stop_check=None):
                await asyncio.sleep(2)
                return ChatResult(content="done")

        buf = EventBuffer()
        host = SubagentHost(_ctx(), parent_runtime=FakeParentRuntime(SlowClient()), buffer=buf)
        host.registry.max_active = 2
        await host.spawn(task_name="a", message="x")
        await host.spawn(task_name="b", message="y")
        from app.subagent.registry import SubagentError
        with pytest.raises(SubagentError):
            await host.spawn(task_name="c", message="z")
        await host.close_all()

    run(flow())


def test_duplicate_name_rejected(isolated):
    async def flow():
        class SlowClient:
            async def run(self, messages, tools=None, on_delta=None, stop_check=None):
                await asyncio.sleep(2)
                return ChatResult(content="done")

        buf = EventBuffer()
        host = SubagentHost(_ctx(), parent_runtime=FakeParentRuntime(SlowClient()), buffer=buf)
        await host.spawn(task_name="dup", message="x")
        from app.subagent.registry import SubagentError
        with pytest.raises(SubagentError, match="仍在运行"):
            await host.spawn(task_name="dup", message="y")
        await host.close_all()

    run(flow())


def test_wait_since_event_seq_incremental(isolated):
    """wait_agent 的 since_event_seq：只返回游标之后的新事件，且 event_seq 单调递增。"""
    async def flow():
        client = FakeClient([
            ChatResult(content="", tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "file_read", "arguments": json.dumps({"path": "x.txt"})},
            }]),
            ChatResult(content="完成"),
        ])
        buf = EventBuffer()
        host = SubagentHost(_ctx(), parent_runtime=FakeParentRuntime(client), buffer=buf)
        await host.spawn(task_name="seq", message="看 x.txt", role="explore")
        first = await host.wait(targets=["seq"], condition="all_terminal", timeout_s=10)
        assert first["timed_out"] is False
        assert first["event_seq"] > 0
        # 首次不带 since：events 为空（只给 event_seq），避免无谓回放
        assert first["events"] == []
        # 用游标再 wait：不会重复返回旧事件
        second = await host.wait(targets=["seq"], condition="all_terminal",
                                 timeout_s=1, since_event_seq=first["event_seq"])
        assert second["events"] == []
        assert second["event_seq"] >= first["event_seq"]
        await host.close_all()

    run(flow())
