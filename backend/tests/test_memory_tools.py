"""M2：记忆三件套工具 + 注册 + 子代理裁剪。

对应 ``docs/20261112-Hlaudz记忆系统实施方案.md`` §7.1 的 M-9，以及 §5 的工具契约。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import memory_store as ms
from app.memory_tools import MEMORY_TOOL_NAMES, build_memory_tools
from app.tool_policy import POLICY_FULL, POLICY_READ_ONLY, filter_tools


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "MEMORIES_DIR", tmp_path / "memories")
    monkeypatch.setattr(ms, "DB_PATH", tmp_path / "app.db")
    run(ms.init_tables())
    return tmp_path


def _tools(user="u1"):
    return {t.name: t for t in build_memory_tools(user)}


# ---------------- 工具面契约 ----------------
def test_exactly_three_tools_no_delete():
    names = {t.name for t in build_memory_tools("u1")}
    assert names == {"memory_list", "memory_read", "memory_write"}
    assert "memory_delete" not in names, "意外承诺了 CC 客户端没承诺的 delete"


def test_tool_meta_flags():
    for t in build_memory_tools("u1"):
        assert t.source == "", "记忆工具是平台内置，不该带 MCP 来源前缀"
        assert t.description
        assert t.input_schema.get("type") == "object"


def test_memory_list_schema_has_cursor():
    tools = _tools()
    props = tools["memory_list"].input_schema["properties"]
    assert "cursor" in props, "缺 cursor 就没法续读超过一屏的记忆（v3 §1.2）"


def test_memory_write_schema_has_if_version():
    props = _tools()["memory_write"].input_schema["properties"]
    assert "if_version" in props


# ---------------- M-9 子代理裁剪 ----------------
def test_m9_child_excludes_memory_tools():
    from app.subagent.capabilities import (CHILD_EXCLUDED, MEMORY_TOOLS,
                                           ORCHESTRATION_TOOLS,
                                           build_capability_profile)

    assert MEMORY_TOOLS == MEMORY_TOOL_NAMES
    assert CHILD_EXCLUDED == ORCHESTRATION_TOOLS | MEMORY_TOOLS

    profile = build_capability_profile(
        available_tools=["file_read", "code_run", "memory_list", "memory_read",
                         "memory_write", "spawn_agent", "load_skill"]
    )
    assert profile.allows("file_read") and profile.allows("code_run")
    assert profile.allows("load_skill")
    for name in MEMORY_TOOL_NAMES:
        assert not profile.allows(name), f"子代理不该拿到 {name}"
    assert not profile.allows("spawn_agent")


def test_m9_build_builtin_tools_can_exclude_memory(monkeypatch, store):
    """``include_memory=False`` 时 registry 里根本没有记忆工具（不是靠 prompt 禁止）。"""
    from app.builtin_tools import build_builtin_tools

    monkeypatch.setenv("HLZ_MEMORY_ENABLED", "1")
    root_names = {t.name for t in build_builtin_tools("u1")}
    child_names = {t.name for t in build_builtin_tools("u1", include_memory=False)}
    assert MEMORY_TOOL_NAMES <= root_names
    assert not (MEMORY_TOOL_NAMES & child_names)


def test_m9_memory_disabled_removes_tools(monkeypatch, store):
    from app.builtin_tools import build_builtin_tools

    monkeypatch.setenv("HLZ_MEMORY_ENABLED", "0")
    names = {t.name for t in build_builtin_tools("u1")}
    assert not (MEMORY_TOOL_NAMES & names)
    # 其它内置工具不受影响
    assert "file_read" in names and "code_run" in names


# ---------------- A2A 档位 ----------------
def test_read_only_policy_keeps_list_and_read_only():
    from app.tools import Tool

    tools = [Tool(n, "", {}, None) for n in
             ("file_read", "list_files", "load_skill", "memory_list",
              "memory_read", "memory_write", "code_run")]
    kept = {t.name for t in filter_tools(tools, POLICY_READ_ONLY)}
    assert kept == {"file_read", "list_files", "load_skill", "memory_list", "memory_read"}
    assert "memory_write" not in kept
    # full 不过滤
    assert {t.name for t in filter_tools(tools, POLICY_FULL)} == {t.name for t in tools}


# ---------------- 工具行为 ----------------
def test_list_empty_message(store):
    out = json.loads(run(_tools()["memory_list"].call({})))
    assert out["entries"] == [] and out["total"] == 0
    assert "还没有任何记忆" in out["note"]


def test_write_then_list_then_read(store):
    tools = _tools("u1")
    w = json.loads(run(tools["memory_write"].call({
        "slug": "user-name",
        "title": "称呼",
        "description": "用户希望被叫老徐",
        "content": "# 称呼\n\n用户希望被叫「老徐」。\n",
        "kind": "user",
        "tags": ["称呼"],
    })))
    assert w["outcome"] == "ok"
    assert w["slug"] == "user-name"
    version = w["version"]
    assert version

    listed = json.loads(run(tools["memory_list"].call({})))
    assert listed["total"] == 1
    assert listed["entries"][0]["slug"] == "user-name"
    assert "content" not in listed["entries"][0], "列举不该带正文"

    read = json.loads(run(tools["memory_read"].call({"slug": "user-name"})))
    assert "老徐" in read["content"]
    assert read["kind"] == "user"


def test_read_missing_slug_returns_readable_error(store):
    out = run(_tools()["memory_read"].call({"slug": "nope"}))
    assert out.startswith("[记忆错误]")
    assert "memory_list" in out


def test_write_without_slug_is_readable_error(store):
    out = run(_tools()["memory_write"].call({"content": "x"}))
    assert out.startswith("[记忆错误]")
    assert "slug" in out


def test_write_without_content_is_readable_error(store):
    out = run(_tools()["memory_write"].call({"slug": "a"}))
    assert out.startswith("[记忆错误]")
    assert "content" in out


def test_write_warns_when_description_missing(store):
    out = json.loads(run(_tools()["memory_write"].call({"slug": "a", "content": "x"})))
    assert out["outcome"] == "ok"
    assert "description" in out["note"]


def test_tools_are_user_scoped(store):
    """同一个进程里两个用户的工具闭包互不可见。"""
    a, b = _tools("userA"), _tools("userB")
    run(a["memory_write"].call({"slug": "secret", "description": "A 的",
                                "content": "只给 A 看"}))
    assert json.loads(run(a["memory_list"].call({})))["total"] == 1
    assert json.loads(run(b["memory_list"].call({})))["total"] == 0
    assert run(b["memory_read"].call({"slug": "secret"})).startswith("[记忆错误]")


def test_write_cas_conflict_through_tool(store):
    tools = _tools("u1")
    first = json.loads(run(tools["memory_write"].call(
        {"slug": "p", "description": "v1", "content": "第一版"})))
    run(tools["memory_write"].call({"slug": "p", "description": "v2", "content": "第二版"}))
    res = json.loads(run(tools["memory_write"].call(
        {"slug": "p", "description": "v3", "content": "第三版",
         "if_version": first["version"]})))
    assert res["outcome"] == "conflict"
    assert "第二版" in res["currentContent"]


def test_read_truncates_by_bytes(store, monkeypatch):
    monkeypatch.setattr(ms, "recall_max_bytes", lambda: 64)
    tools = _tools("u1")
    run(tools["memory_write"].call(
        {"slug": "long", "description": "长", "content": "中" * 200}))
    out = json.loads(run(tools["memory_read"].call({"slug": "long"})))
    assert out.get("truncated") is True
    assert len(out["content"].encode("utf-8")) <= 64
    assert "note" in out


def test_list_pagination_through_tool(store):
    tools = _tools("u1")
    for i in range(55):
        run(tools["memory_write"].call(
            {"slug": f"m{i:03d}", "description": f"第 {i} 条", "content": "x"}))
    page1 = json.loads(run(tools["memory_list"].call({})))
    assert len(page1["entries"]) == 50 and page1["remaining"] == 5
    page2 = json.loads(run(tools["memory_list"].call({"cursor": page1["next_cursor"]})))
    assert len(page2["entries"]) == 5 and page2["remaining"] == 0


def test_list_query_filter(store):
    tools = _tools("u1")
    run(tools["memory_write"].call({"slug": "pdf", "description": "关于 PDF 的", "content": "x"}))
    run(tools["memory_write"].call({"slug": "speed", "description": "关于速度的", "content": "x"}))
    out = json.loads(run(tools["memory_list"].call({"query": "PDF"})))
    assert [e["slug"] for e in out["entries"]] == ["pdf"]
