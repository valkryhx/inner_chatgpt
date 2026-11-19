"""MCP 会话自愈 / 同名工具冲突 / 结果预算 —— 2026-11-10 真实验证发现的缺陷回归。

三个缺陷都来自同一轮真实 e2e（work/mcp_stale、work/mcp_e2e）：

1. **会话僵死**：MCP server 重启后，缓存 runtime 里的 fastmcp client 报
   `Session terminated`，但 TTL 探测用**新**连接看到「已连接、12 工具」，
   与缓存 manifest 一致 → 判定「状态没变」不重建。坏 client 永远留在缓存里，
   用户每轮调用都失败，只能重启后端。
   → 修复：`MCPToolset.reconnect()` + 工具调用命中传输类错误时自动重连重试一次。

2. **同名工具冲突**：两个 server 都暴露 `health` 时，registry 里出现两个同名 function，
   `dispatch()` 永远命中最后注册的那个 —— 静默调错 server。
   → 修复：`merge()` 给冲突工具加 server 前缀，并记进 `_renamed` 供 manifest 披露。

3. **MCP 结果没有预算**：内置工具有 per-tool 上限 + 超限落盘，MCP 完全没有。
   实测 `run_readonly_sql` 一次返回 115 KB，直接塞进 history。
   → 修复：MCP 结果统一走 `tool_budget.apply_budget`。
"""
from __future__ import annotations

import asyncio

import pytest

from app import mcp_client
from app.mcp_client import MCPToolset, _looks_session_dead
from app.tools import ToolRegistry

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def run(coro):
    return asyncio.run(coro)


def _raw(name, description="d"):
    return type("T", (), {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": {}},
    })()


class ScriptedClient:
    """按脚本返回结果/抛异常的 client 替身。"""

    def __init__(self, tools, script=None, connect_error=None):
        self._tools = tools
        self._script = list(script or [])
        self._connect_error = connect_error
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        if self._connect_error:
            raise self._connect_error
        return self

    async def __aexit__(self, *exc):
        return None

    async def list_tools(self):
        return self._tools

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self._script:
            step = self._script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        return {"ok": True, "name": name}


def _toolset(server, names, *, script=None):
    ts = MCPToolset({"transport": "http", "url": "http://example.invalid/mcp"},
                    user_id="u1", server_name=server)
    ts._client = ScriptedClient([_raw(n) for n in names], script=script)
    ts._raw = [_raw(n) for n in names]
    ts._tools = [ts._wrap(t) for t in ts._raw]
    return ts


# ---------------- 1. 会话失效判定 ----------------
@pytest.mark.parametrize("msg", [
    "Session terminated",
    "McpError: Session terminated",
    "Connection closed by peer",
    "httpx.ConnectError: All connection attempts failed",
    "ClientConnectorError: Cannot connect to host",
    "ServerDisconnectedError: Server disconnected",
    "Broken pipe",
])
def test_transport_errors_are_detected_as_dead_session(msg):
    assert _looks_session_dead(RuntimeError(msg)) is True


@pytest.mark.parametrize("msg", [
    "validation error for metric_series",
    "unexpected keyword argument 'foo'",
    "工具业务错误：该指标不存在",
    "Unknown tool: nope",
])
def test_business_errors_are_not_treated_as_dead_session(msg):
    """参数错/业务错重连也没用，不该触发重连。"""
    assert _looks_session_dead(ValueError(msg)) is False


# ---------------- 1b. 「从没连上」也要能自愈（2026-11-19 真机复现） ----------------
#
# 现场：exa MCP 的 3 个工具全部报
#   RuntimeError: Client is not connected. Use the 'async with client:' ...
# 而且重试 3 次都一样 —— 因为这条错误不在 `_SESSION_DEAD_MARKERS` 里，
# `_looks_session_dead` 返回 False，**重连根本没被触发**。
#
# 这不是网络错误，是 fastmcp 的本地会话状态错误：Client 对象从没成功
# `__aenter__` 过（连接在启动时失败 / 被 invalidate 关掉后重建又失败）。
# 后果比「会话断了」更严重 —— 它不会随时间自愈，整个进程生命周期内
# 这个 server 都不可用，且日志里没有任何重连尝试。

@pytest.mark.parametrize("msg", [
    "Client is not connected. Use the 'async with client:' context manager first.",
    "RuntimeError: Client is not connected",
    "not connected",                       # 大小写/前缀不同的变体
    "MCP server not connected yet",
])
def test_not_connected_is_detected_as_dead_session(msg):
    """**核心**：「从没连上」必须和「会话断了」一样触发重连。

    两者的恢复手段完全相同（重建 client），所以判定必须都返回 True。
    """
    assert _looks_session_dead(RuntimeError(msg)) is True, (
        f"{msg!r} 没被判定为可重连 —— 该 server 会永久不可用")


def test_tool_call_reconnects_when_never_connected(monkeypatch):
    """**核心回归**：首次调用报 `Client is not connected`，必须重连并成功。

    复现 hx 用户的 exa：三个工具全部失败，重试也没用。
    """
    ts = _toolset("exa", ["web_search_exa"], script=[
        RuntimeError("Client is not connected. Use the 'async with client:' "
                     "context manager first.")])
    fresh = ScriptedClient([_raw("web_search_exa")])

    monkeypatch.setattr(mcp_client, "Client", lambda target: fresh)

    out = run(ts.tools[0].call({"query": "x"}))
    assert "工具执行错误" not in out, f"没有自愈：{out}"
    assert fresh.calls == [("web_search_exa", {"query": "x"})]


def test_never_connected_is_not_a_business_error(monkeypatch):
    """`Client is not connected` 绝不能走「参数校验失败」那条分支。

    那条分支会给模型一个误导提示（「请严格按工具参数定义调用」），
    让模型反复改参数而永远调不通。
    """
    called = {"n": 0}

    def fake_client(target):
        called["n"] += 1
        return ScriptedClient([_raw("ping")])

    ts = _toolset("exa", ["ping"], script=[RuntimeError("Client is not connected")])
    monkeypatch.setattr(mcp_client, "Client", fake_client)
    out = run(ts.tools[0].call({}))
    assert "参数校验失败" not in out, f"被当成参数错了：{out}"
    assert called["n"] == 1, "没有尝试重连"


# ---------------- 2. 自动重连重试 ----------------
def test_tool_call_reconnects_once_when_session_dies(monkeypatch):
    """核心回归：第一次调用抛 Session terminated，应重连并重试成功。"""
    ts = _toolset("tmpsrv", ["ping"], script=[RuntimeError("Session terminated")])
    fresh = ScriptedClient([_raw("ping")])

    def fake_client(target):
        return fresh

    monkeypatch.setattr(mcp_client, "Client", fake_client)

    out = run(ts.tools[0].call({}))
    assert "工具执行错误" not in out
    assert fresh.calls == [("ping", {})]


def test_tool_call_reports_when_reconnect_also_fails(monkeypatch):
    ts = _toolset("tmpsrv", ["ping"], script=[RuntimeError("Session terminated")])
    monkeypatch.setattr(mcp_client, "Client",
                        lambda target: ScriptedClient([], connect_error=RuntimeError("still down")))
    out = run(ts.tools[0].call({}))
    assert "自动重连失败" in out
    assert "still down" in out


def test_business_error_does_not_trigger_reconnect(monkeypatch):
    called = {"n": 0}

    def fake_client(target):
        called["n"] += 1
        return ScriptedClient([_raw("ping")])

    monkeypatch.setattr(mcp_client, "Client", fake_client)
    ts = _toolset("tmpsrv", ["ping"], script=[ValueError("validation error: bad arg")])
    out = run(ts.tools[0].call({}))
    assert "参数校验失败" in out
    assert called["n"] == 0          # 没有重连


def test_reconnect_refreshes_tools_and_clears_error(monkeypatch):
    ts = _toolset("tmpsrv", ["ping"])
    monkeypatch.setattr(mcp_client, "Client",
                        lambda target: ScriptedClient([_raw("ping"), _raw("pong")]))
    assert run(ts.reconnect()) is True
    assert [t.name for t in ts.tools] == ["ping", "pong"]
    assert ts._error is None


# ---------------- 3. 跨 server 同名工具 ----------------
def test_duplicate_tool_names_are_disambiguated_on_merge():
    a = _toolset("sqlbotmcp", ["health", "metric_search"])
    b = _toolset("othermcp", ["health", "search"])
    merged = MCPToolset.merge([a, b])
    names = [t.name for t in merged.tools]
    assert len(names) == len(set(names)), names
    assert "health" in names                       # 首个保留原名
    assert "othermcp_health" in names              # 冲突者加前缀
    assert merged._renamed == [{"server": "othermcp", "original": "health",
                                "assigned": "othermcp_health"}]


def test_merged_registry_schemas_have_unique_function_names():
    a = _toolset("s1", ["health"])
    b = _toolset("s2", ["health"])
    merged = MCPToolset.merge([a, b])
    schemas = merged and ToolRegistry(merged.tools).schemas()
    names = [s["function"]["name"] for s in schemas]
    assert len(names) == len(set(names))


def test_duplicate_tool_dispatch_reaches_the_right_server():
    """修复前 dispatch('health') 永远命中最后注册的 server（静默调错）。"""
    a = _toolset("s1", ["health"])
    b = _toolset("s2", ["health"])
    merged = MCPToolset.merge([a, b])
    reg = ToolRegistry(merged.tools)
    second = [t for t in merged.tools if t.name == "s2_health"][0]
    assert second.source == "MCP: s2"
    assert reg.get("s2_health") is second
    assert reg.get("health").source == "MCP: s1"


def test_no_renaming_when_names_do_not_collide():
    a = _toolset("s1", ["a1", "a2"])
    b = _toolset("s2", ["b1"])
    merged = MCPToolset.merge([a, b])
    assert [t.name for t in merged.tools] == ["a1", "a2", "b1"]
    assert merged._renamed == []


def test_renamed_tools_keep_source_for_model():
    a = _toolset("s1", ["health"])
    b = _toolset("s2", ["health"])
    merged = MCPToolset.merge([a, b])
    renamed = [t for t in merged.tools if t.name == "s2_health"][0]
    assert renamed.source == "MCP: s2"
    assert renamed.to_openai_schema()["function"]["description"].startswith("[MCP: s2]")


# ---------------- 4. MCP 结果预算 ----------------
def test_large_mcp_result_is_budgeted(monkeypatch, tmp_path):
    """MCP 结果超限时必须裁剪并落盘，不能把 115 KB 原样塞进 history。"""
    monkeypatch.setenv("HLZ_TOOL_RESULT_MAX_CHARS", "200")
    monkeypatch.setattr("app.workspace.user_workspace", lambda uid, create=True: tmp_path)
    big = "x" * 5000
    ts = _toolset("s1", ["dump"], script=[big])
    ts._user_id = "u1"
    out = run(ts.tools[0].call({}))
    assert len(out) < len(big)
    assert "chars omitted" in out
    # 落盘文件可回读
    files = list((tmp_path / ".hlaudz" / "tool_results").glob("*.txt"))
    assert files and files[0].read_text(encoding="utf-8").startswith("x" * 100)


def test_small_mcp_result_is_untouched(monkeypatch, tmp_path):
    monkeypatch.setenv("HLZ_TOOL_RESULT_MAX_CHARS", "50000")
    monkeypatch.setattr("app.workspace.user_workspace", lambda uid, create=True: tmp_path)
    ts = _toolset("s1", ["ping"], script=["pong"])
    ts._user_id = "u1"
    assert run(ts.tools[0].call({})) == "pong"
