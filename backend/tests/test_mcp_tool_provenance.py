"""MCP 工具来源可见性（2026-11-10 hlaudz 误报「没有 sqlbot 工具」的回归测试）。

缺陷：多 server 的工具被拍平进同一个 list，name 没有命名空间，Tool 也不记录来源。
模型因此只能按名字猜归属——hx 会话里它**实际调用了** metric_search / health，
却在同一条回复里声称「没有 sqlbot 工具」，并把 workspace 里旧脚本的注释当证据。

修复后必须成立的三条不变量：
1. 每个 MCP 工具都带 source（"MCP: <server name>"），并出现在描述前缀里；
2. 系统提示词有权威的 server 清单（连接成功 / 失败原因 / 工具名）；
3. 连接失败不再静默消失，而是以 ok=False + 原因进入清单。
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent import format_mcp_manifest
from app.mcp_client import MCPToolset
from app.tools import Tool, ToolRegistry

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    """够用的 MCP client 替身：list_tools / call_tool / async context。"""

    def __init__(self, tools):
        self._tools = tools

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def list_tools(self):
        return self._tools

    async def call_tool(self, name, args):
        return {"ok": True, "name": name}


def _raw(name, description="d"):
    return type("T", (), {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": {}},
    })()


def _toolset(server_name, tools):
    ts = MCPToolset({"transport": "http", "url": "http://example.invalid/mcp"},
                    user_id="u1", server_name=server_name)
    ts._client = FakeClient(tools)
    ts._raw = list(tools)
    ts._tools = [ts._wrap(t) for t in tools]
    return ts


# ---------------- 1. 来源标识 ----------------
def test_mcp_tools_carry_server_source():
    ts = _toolset("sqlbotmcp", [_raw("metric_search"), _raw("run_readonly_sql")])
    assert [t.source for t in ts.tools] == ["MCP: sqlbotmcp", "MCP: sqlbotmcp"]


def test_source_appears_in_description_sent_to_model():
    ts = _toolset("sqlbotmcp", [_raw("metric_search", "按中文名模糊搜索指标")])
    desc = ts.tools[0].to_openai_schema()["function"]["description"]
    assert desc.startswith("[MCP: sqlbotmcp]")
    assert "模糊搜索指标" in desc


def test_source_prefix_is_not_duplicated():
    t = Tool("x", "[MCP: exa] already tagged", {"type": "object", "properties": {}}, None,
             source="MCP: exa")
    assert t.to_openai_schema()["function"]["description"] == "[MCP: exa] already tagged"


def test_builtin_tools_have_no_source_prefix():
    async def call(args):
        return "ok"

    t = Tool("file_read", "读取文件", {"type": "object", "properties": {}}, call)
    schema = t.to_openai_schema()["function"]
    assert schema["description"] == "读取文件"


def test_tool_names_are_unchanged_for_contract_stability():
    """来源只进 description；工具名是 SSE / 前端 / 历史共同的契约，必须原样。"""
    ts = _toolset("exa", [_raw("web_search_exa")])
    assert ts.tools[0].to_openai_schema()["function"]["name"] == "web_search_exa"


# ---------------- 2. 系统提示词清单 ----------------
def test_manifest_lists_connected_servers_with_tool_names():
    ts1 = _toolset("sqlbotmcp", [_raw("metric_search"), _raw("metric_series")])
    ts2 = _toolset("exa", [_raw("web_search_exa")])
    merged = MCPToolset.merge([ts1, ts2])

    block = format_mcp_manifest(merged.manifest)
    assert "sqlbotmcp" in block
    assert "metric_search" in block
    assert "exa" in block
    assert "connection FAILED" not in block


def test_manifest_reports_connected_tool_count():
    ts = _toolset("sqlbotmcp", [_raw("a"), _raw("b"), _raw("c")])
    (entry,) = ts.manifest
    assert entry["ok"] is True
    assert entry["tool_count"] == 3
    assert entry["tools"] == ["a", "b", "c"]


def test_manifest_surfaces_failed_server_instead_of_hiding_it():
    """连接失败必须在提示词里显式出现，模型才不会编造「本来就没有」。"""
    manifest = [{
        "name": "sqlbotmcp", "ok": False, "tool_count": 0,
        "error": "RuntimeError: All connection attempts failed", "tools": [],
    }]
    block = format_mcp_manifest(manifest)
    assert "sqlbotmcp" in block
    assert "connection FAILED" in block
    assert "All connection attempts failed" in block


def test_manifest_empty_when_no_servers():
    assert format_mcp_manifest(None) == ""
    assert format_mcp_manifest([]) == ""


# ---------------- 3. 连接失败可见 ----------------
def test_connect_failure_sets_error_and_status_ok_false():
    ts = MCPToolset({"transport": "http", "url": "http://127.0.0.1:9/mcp"},
                    user_id="u1", server_name="deadmcp")
    run(ts.__aenter__())
    entry = ts.status()
    assert entry["ok"] is False
    assert entry["tool_count"] == 0
    assert entry["error"]
    assert entry["name"] == "deadmcp"


def test_status_ok_true_when_tools_discovered():
    ts = _toolset("sqlbotmcp", [_raw("metric_search")])
    entry = ts.status()
    assert entry["ok"] is True
    assert entry["tool_count"] == 1


# ---------------- 4. 子代理继承来源 ----------------
def test_child_registry_preserves_source():
    from app.subagent.capabilities import build_capability_profile, build_child_registry

    ts = _toolset("sqlbotmcp", [_raw("metric_search")])
    parent = ToolRegistry(list(ts.tools))
    profile = build_capability_profile(available_tools=parent.names())
    child = build_child_registry(parent, profile)

    (tool,) = child.tools
    assert tool.source == "MCP: sqlbotmcp"
    assert tool.to_openai_schema()["function"]["description"].startswith("[MCP: sqlbotmcp]")
