"""`/api/mcp/tools` 的来源契约。

用户与模型必须看到**同一份**工具描述：带 `[MCP: <server>]` 前缀。
2026-11-10 真实 e2e 发现：该端点返回原始 description，
于是设置页/工具面板里 sqlbot 的工具看不出属于哪个 server，
与对话中模型看到的版本不一致，用户据此判断归属会被误导。
"""
from __future__ import annotations

from app.tools import Tool, ToolRegistry


def _tool(name, desc, source=""):
    async def call(args):
        return "ok"

    return Tool(name=name, description=desc,
                input_schema={"type": "object", "properties": {}},
                call=call, source=source)


def test_mcp_tool_row_uses_model_facing_description():
    """端点用 describe_for_model()：带来源前缀，并保留原始描述与 source 字段。"""
    mcp_tool = _tool("metric_search", "按中文名模糊搜索指标", source="MCP: sqlbotmcp")
    builtin = _tool("file_read", "读取文件")
    reg = ToolRegistry([builtin, mcp_tool])

    rows = {}
    for t in reg.tools:
        rows[t.name] = {
            "name": t.name,
            "description": t.describe_for_model(),
            "raw_description": t.description,
            "source": t.source,
        }

    assert rows["metric_search"]["description"] == "[MCP: sqlbotmcp] 按中文名模糊搜索指标"
    assert rows["metric_search"]["raw_description"] == "按中文名模糊搜索指标"
    assert rows["metric_search"]["source"] == "MCP: sqlbotmcp"
    # 内置工具不加前缀
    assert rows["file_read"]["description"] == "读取文件"
    assert rows["file_read"]["source"] == ""


def test_renamed_conflict_tool_keeps_its_source_on_the_endpoint():
    """同名冲突改名后，端点仍要能看出它属于哪个 server。"""
    from app.mcp_client import MCPToolset

    class FakeClient:
        def __init__(self, tools):
            self._tools = tools

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def list_tools(self):
            return self._tools

    def raw(name):
        return type("T", (), {"name": name, "description": "健康检查",
                              "inputSchema": {"type": "object", "properties": {}}})()

    def make(server):
        ts = MCPToolset({"transport": "http", "url": "http://x.invalid/mcp"},
                        user_id="u1", server_name=server)
        ts._client = FakeClient([raw("health")])
        ts._raw = [raw("health")]
        ts._tools = [ts._wrap(t) for t in ts._raw]
        return ts

    merged = MCPToolset.merge([make("s1"), make("s2")])
    by_name = {t.name: t for t in merged.tools}
    assert by_name["health"].describe_for_model().startswith("[MCP: s1]")
    assert by_name["s2_health"].describe_for_model().startswith("[MCP: s2]")
    assert by_name["s2_health"].source == "MCP: s2"
