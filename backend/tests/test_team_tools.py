"""Tool surface and scope-closure tests for Agent Team."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from app import team_store
from app.scope import TeamScope
from app.team_host import TeamHost
from app.team_tools import (
    LEAD_TOOL_NAMES,
    TEAMMATE_TOOL_NAMES,
    build_lead_tools,
    build_team_tool_registry,
    build_teammate_tools,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "team.db"
    monkeypatch.setattr(team_store, "DB_PATH", path)
    run(team_store.init_tables())
    return path


def scope(uid="u1", sid="s1", gid="g1", tid="t1", agent="root", role="lead"):
    return TeamScope(uid, sid, gid, tid, agent, role)


class FakeQuota:
    def release(self):
        pass

    async def team_lease(self, sc, *, timeout_s=0.0):
        return self

    @asynccontextmanager
    async def team(self, sc, *, timeout_s=0.0):
        yield await self.team_lease(sc, timeout_s=timeout_s)


def test_lead_and_teammate_exposure_is_closed_world():
    lead = TeamHost(scope(), quota_manager=FakeQuota())
    mate = TeamHost(scope(agent="sub:a", role="teammate"), quota_manager=FakeQuota())
    lead_names = {t.name for t in build_lead_tools(lead)}
    mate_names = {t.name for t in build_teammate_tools(mate)}
    assert lead_names == LEAD_TOOL_NAMES
    assert mate_names == TEAMMATE_TOOL_NAMES
    assert "task_create" not in mate_names
    assert "spawn_teammate" not in mate_names
    assert "team_status" not in mate_names


def test_tool_arguments_cannot_override_scope(isolated_db):
    async def flow():
        lead = TeamHost(scope(), quota_manager=FakeQuota())
        await lead.create("demo")
        registry = build_team_tool_registry(lead)
        # malicious identity/session fields are ignored by the closure
        raw = await registry.dispatch("task_create", {
            "subject": "mine", "user_id": "other", "session_id": "other",
            "generation_id": "other", "team_id": "other",
        })
        task = json.loads(raw)
        assert task["user_id"] == "u1"
        assert task["session_id"] == "s1"
        assert task["team_id"] == "t1"

    run(flow())


def test_teammate_registry_cannot_create_or_spawn(isolated_db):
    async def flow():
        lead = TeamHost(scope(), quota_manager=FakeQuota())
        await lead.create("demo")
        mate = TeamHost(scope(agent="sub:a", role="teammate"), quota_manager=FakeQuota())
        reg = build_team_tool_registry(mate)
        assert "未知工具" in await reg.dispatch("task_create", {"subject": "x"})
        assert "未知工具" in await reg.dispatch("spawn_teammate", {"task_name": "x", "message": "y"})

    run(flow())


def test_teammate_task_update_cas_and_cannot_message_peer(isolated_db):
    async def flow():
        lead = TeamHost(scope(), quota_manager=FakeQuota())
        await lead.create("demo")
        task = await lead.create_task(subject="x")
        await lead.bind_task_owner(task["task_id"], "sub:a", expected_version=0)
        mate = TeamHost(scope(agent="sub:a", role="teammate"), quota_manager=FakeQuota())
        reg = build_team_tool_registry(mate)
        out = json.loads(await reg.dispatch("task_update", {
            "task_id": task["task_id"], "action": "complete", "expected_version": 1,
        }))
        assert out["ok"] is True
        refused = await reg.dispatch("send_message", {
            "recipient": "sub:b", "content": "peer message",
        })
        assert "only message the lead" in refused

    run(flow())


def test_spawn_protocol_passes_server_scope_and_binds_owner(isolated_db):
    async def flow():
        calls = []

        async def spawn_fn(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "agent_id": "sub:a"}

        host = TeamHost(scope(), quota_manager=FakeQuota(), spawn_fn=spawn_fn)
        await host.create("demo")
        task = await host.create_task(subject="x")
        reg = build_team_tool_registry(host)
        out = json.loads(await reg.dispatch("spawn_teammate", {
            "task_name": "a", "message": "go", "team_task_id": task["task_id"],
            "expected_version": 0, "user_id": "malicious",
        }))
        assert out["ok"] is True
        assert out["task"]["owner_agent_id"] == "sub:a"
        assert calls[0]["scope"].user_id == "u1"
        assert calls[0]["owner_agent_id"] == "sub:a"

    run(flow())


def test_messages_are_recipient_filtered_by_tool() -> None:
    class StubHost:
        role = "teammate"
        scope = scope(agent="sub:a", role="teammate")
        async def list_messages(self, **kwargs):
            self.kwargs = kwargs
            return []

    host = StubHost()
    tools = build_teammate_tools(host)  # type: ignore[arg-type]
    tool = next(t for t in tools if t.name == "list_messages")
    run(tool.call({}))
    assert host.kwargs["recipient"] == "sub:a"
