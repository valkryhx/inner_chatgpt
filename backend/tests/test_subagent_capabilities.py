"""S2: capabilities / registry / runtime assembly."""
from __future__ import annotations

import asyncio

import pytest

from app.tools import Tool, ToolRegistry
from app.subagent.capabilities import (
    ORCHESTRATION_TOOLS,
    READ_ONLY_TOOLS,
    build_capability_profile,
    build_child_registry,
)
from app.subagent.context import AgentContext
from app.subagent.registry import (
    SubagentError,
    SubagentRegistry,
    validate_task_name,
)


def _dummy_registry(names):
    def make(n):
        async def call(args):
            return "ok"
        return Tool(n, "", {"type": "object", "properties": {}}, call)
    return ToolRegistry([make(n) for n in names])


# ---------------- capabilities ----------------
def test_general_role_has_no_orchestration_tools():
    available = ["file_read", "file_write", "code_run", "load_skill",
                 "spawn_agent", "wait_agent", "list_agents", "close_agent"]
    p = build_capability_profile(role="general", available_tools=available)
    assert "spawn_agent" not in p.allowed
    assert "wait_agent" not in p.allowed
    assert {"file_read", "file_write", "code_run", "load_skill"} <= set(p.allowed)


def test_readonly_roles_only_get_read_tools():
    available = ["file_read", "file_write", "file_patch", "code_run", "list_files", "load_skill", "spawn_agent"]
    for role in ("explore", "plan", "review"):
        p = build_capability_profile(role=role, available_tools=available)
        assert p.allowed == frozenset(available) & READ_ONLY_TOOLS, role
        assert "file_write" not in p.allowed
        assert "code_run" not in p.allowed


def test_allow_delegation_only_when_explicit():
    available = ["file_read", "spawn_agent", "wait_agent"]
    p = build_capability_profile(role="general", available_tools=available, allow_delegation=True)
    assert "spawn_agent" in p.allowed
    p2 = build_capability_profile(role="general", available_tools=available)
    assert "spawn_agent" not in p2.allowed


def test_unknown_role_falls_back_to_general():
    p = build_capability_profile(role="hacker", available_tools=["file_read"])
    assert p.role == "general"


def test_build_child_registry_filters():
    parent = _dummy_registry(["file_read", "file_write", "spawn_agent", "code_run"])
    p = build_capability_profile(role="explore", available_tools=parent.names())
    child = build_child_registry(parent, p)
    assert "file_read" in child.names()
    assert "file_write" not in child.names()
    assert "spawn_agent" not in child.names()


# ---------------- registry ----------------
def _ctx(depth=0):
    return AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1", depth=depth)


def test_task_name_validation():
    assert validate_task_name("Scan_A1") == "scan_a1"
    for bad in ("", "ab-c", "a b", "团队", "x" * 40, "root", "team-lead", "_x"):
        with pytest.raises(SubagentError):
            validate_task_name(bad)


def test_depth_limit():
    reg = SubagentRegistry(max_depth=1, max_active=4)
    c = reg.create_child(_ctx(0), "a", role="general")
    with pytest.raises(SubagentError, match="最大深度"):
        reg.create_child(c.ctx, "b", role="general")


def test_name_conflict_while_alive():
    reg = SubagentRegistry(max_depth=2, max_active=4)
    reg.create_child(_ctx(), "a", role="general")
    with pytest.raises(SubagentError, match="仍在运行"):
        reg.create_child(_ctx(), "a", role="general")


def test_same_name_reusable_after_terminal():
    reg = SubagentRegistry(max_depth=2, max_active=4)
    node = reg.create_child(_ctx(), "a", role="general")
    reg.mark_terminal(node, "completed", result="done")
    again = reg.create_child(_ctx(), "a", role="general")
    assert again is not node
    assert again.status == "starting"


def test_active_slot_limit_and_eviction():
    reg = SubagentRegistry(max_depth=2, max_active=2)
    a = reg.create_child(_ctx(), "a", role="general")
    reg.create_child(_ctx(), "b", role="general")
    # both alive -> third is rejected
    with pytest.raises(SubagentError, match="上限"):
        reg.create_child(_ctx(), "c", role="general")
    # finishing one frees a slot (terminal nodes get evicted when needed)
    reg.mark_terminal(a, "completed", result="x")
    c = reg.create_child(_ctx(), "c", role="general")
    assert c.name == "c"


def test_notify_waiters():
    async def flow():
        reg = SubagentRegistry()
        node = reg.create_child(_ctx(), "a", role="general")
        ev = asyncio.Event()
        node.waiters.append(ev)
        reg.mark_terminal(node, "completed", result="ok")
        assert ev.is_set()
        assert node.notify_waiters  # no crash when empty

    asyncio.run(flow())
