"""S2: capabilities / registry / runtime assembly."""
from __future__ import annotations

import asyncio

import pytest

from app.tools import Tool, ToolRegistry
from app.subagent.capabilities import (
    ORCHESTRATION_TOOLS,
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
def test_child_keeps_all_business_tools():
    available = [
        "file_read", "file_write", "file_patch", "list_files", "code_run", "load_skill",
        "mcp_sqlbot_query", "mcp_web_search",
        "spawn_agent", "wait_agent", "read_agent_result", "list_agents", "close_agent",
    ]
    p = build_capability_profile(available_tools=available)
    assert p.allowed == frozenset(available) - ORCHESTRATION_TOOLS
    # 子代理是完整工作代理：代码执行 / 写文件 / Skill / MCP 都保留
    assert {"file_write", "code_run", "load_skill", "mcp_sqlbot_query", "mcp_web_search"} <= p.allowed
    # 唯一硬边界：编排工具不可见
    assert not (p.allowed & ORCHESTRATION_TOOLS)


def test_child_registry_filters_only_orchestration():
    parent = _dummy_registry([
        "file_read", "file_write", "code_run", "load_skill", "mcp_demo",
        "spawn_agent", "wait_agent", "task_create", "team_status",
    ])
    p = build_capability_profile(available_tools=parent.names())
    child = build_child_registry(parent, p)
    names = set(child.names())
    assert {"file_read", "file_write", "code_run", "load_skill", "mcp_demo"} <= names
    assert not (names & ORCHESTRATION_TOOLS)


def test_empty_parent_registry_is_safe():
    p = build_capability_profile(available_tools=[])
    assert p.allowed == frozenset()
    child = build_child_registry(ToolRegistry(), p)
    assert child.names() == []


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
    c = reg.create_child(_ctx(0), "a")
    with pytest.raises(SubagentError, match="最大深度"):
        reg.create_child(c.ctx, "b")


def test_name_conflict_while_alive():
    reg = SubagentRegistry(max_depth=2, max_active=4)
    reg.create_child(_ctx(), "a")
    with pytest.raises(SubagentError, match="仍在运行"):
        reg.create_child(_ctx(), "a")


def test_same_name_reusable_after_terminal():
    reg = SubagentRegistry(max_depth=2, max_active=4)
    node = reg.create_child(_ctx(), "a")
    reg.mark_terminal(node, "completed", result="done")
    again = reg.create_child(_ctx(), "a")
    assert again is not node
    assert again.status == "starting"


def test_active_slot_limit_and_eviction():
    reg = SubagentRegistry(max_depth=2, max_active=2)
    a = reg.create_child(_ctx(), "a")
    reg.create_child(_ctx(), "b")
    with pytest.raises(SubagentError, match="上限"):
        reg.create_child(_ctx(), "c")
    reg.mark_terminal(a, "completed", result="x")
    c = reg.create_child(_ctx(), "c")
    assert c.name == "c"


def test_notify_waiters():
    async def flow():
        reg = SubagentRegistry()
        node = reg.create_child(_ctx(), "a")
        ev = asyncio.Event()
        node.waiters.append(ev)
        reg.mark_terminal(node, "completed", result="ok")
        assert ev.is_set()
        assert node.notify_waiters  # no crash when empty

    asyncio.run(flow())
