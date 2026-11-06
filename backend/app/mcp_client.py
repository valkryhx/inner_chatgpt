"""MCP 客户端：把 FastMCP 工具转换成自研 Tool。

fastmcp.Client 直接接入，封装为自研 Tool（不再依赖 langchain）。
支持按「目标」构建：
- target 为 FastMCP 实例（内置 demo）-> in-process
- dict: {"transport": "http"/"sse", "url":..., "headers":...}
- dict: {"transport": "stdio", "command":...}
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastmcp import Client
from .tools import Tool


def _result_to_text(result: Any) -> str:
    data = getattr(result, "data", None)
    if data is not None:
        if isinstance(data, (dict, list)):
            return json.dumps(data, ensure_ascii=False)
        return str(data)
    content = getattr(result, "content", None)
    if content:
        parts = []
        for b in content:
            text = getattr(b, "text", None)
            parts.append(str(text) if text is not None else str(b))
        return "\n".join(parts)
    return str(result)


def _schema_props(schema: Any) -> list[str]:
    """从工具 inputSchema 里尽量抽出可用参数名，便于报错时提示模型。"""
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if isinstance(props, dict):
        return list(props.keys())
    return []


async def _call_tool_safe(client: Any, name: str, kwargs: dict[str, Any], schema: Any) -> str:
    """调用 MCP 工具，任何异常都转成可读文本返回（永不冒泡）。

    目的：LLM 能力有限，某个工具参数写错是常态；应把错误作为「观察」回喂模型，
    让它按提示改参/换工具继续（ReAct），而不是中断整轮、留下悬空 tool_call。
    """
    try:
        result = await client.call_tool(name, kwargs)
        return _result_to_text(result)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        props = _schema_props(schema)
        hint = ""
        low = msg.lower()
        if ("validation" in low) or ("unexpected keyword" in low) or ("argument" in low):
            hint = (
                "\n[参数校验失败] 请严格按工具参数定义调用，不要臆造参数名。"
                f"本次可用参数: {props}。"
                "若需按时间过滤，注意各工具参数不同：metric_latest 只取最新值，"
                "指定某月请用 metric_series 的 month=YYYY-MM。"
            )
        return f"[工具执行错误] {type(e).__name__}: {msg}{hint}"


def _build_target(target: Any) -> Any:
    """把高层描述转成 fastmcp.Client 接受的 transport。

    兼容标准 mcp.json 的字段：stdio 用 command/args/env；http/sse 用 url/headers。
    """
    if not isinstance(target, dict):
        return target  # FastMCP 实例或 URL 字符串
    transport = (target.get("transport") or "http").lower()
    if transport == "stdio":
        from fastmcp.client.transports import StdioTransport
        command = (target.get("command") or "").strip()
        args = target.get("args")
        if isinstance(args, list) and args:
            parts = [str(x) for x in args]
            exe = command or (parts.pop(0) if parts else "")
            if not exe:
                raise ValueError("stdio mcp requires command")
            return StdioTransport(command=exe, args=parts, env=target.get("env") or None)
        # 无 args：把整条 command 拆成 [exe, *args]
        parts = command.split()
        if not parts:
            raise ValueError("stdio mcp requires command")
        return StdioTransport(command=parts[0], args=parts[1:], env=target.get("env") or None)
    if transport == "sse":
        from fastmcp.client.transports import SSETransport
        return SSETransport(url=target.get("url", ""), headers=target.get("headers") or None)
    # 默认 http（streamable-http）
    from fastmcp.client.transports import StreamableHttpTransport
    return StreamableHttpTransport(url=target.get("url", ""), headers=target.get("headers") or None)


class MCPToolset:
    """持有一个 fastmcp.Client，暴露为自研 Tool 列表（必须在 lifespan 内 enter/exit）。"""

    def __init__(self, target: Any = None, *, user_id: str = "") -> None:
        # target None -> 内置 demo
        self._target = target
        self._user_id = user_id
        self._client: Optional[Client] = None
        self._tools: list[Tool] = []
        self._raw: list[Any] = []
        self._error: Optional[str] = None
        # 合并后：持有一组子 toolset，关闭时统一释放
        self._children: list["MCPToolset"] = []

    @classmethod
    def merge(cls, toolsets: list["MCPToolset"]) -> "MCPToolset":
        """聚合多个已连接的 toolset 的工具；关闭父级时统一释放全部子级。"""
        user_ids = {ts._user_id for ts in toolsets if ts._user_id}
        merged = cls(None, user_id=next(iter(user_ids)) if len(user_ids) == 1 else "")
        merged._children = list(toolsets)
        for ts in toolsets:
            merged._tools.extend(ts.tools)
            merged._raw.extend(ts._raw)
        return merged

    async def __aenter__(self) -> "MCPToolset":
        try:
            self._client = Client(_build_target(self._target))
            await self._client.__aenter__()
            await self._discover()
        except Exception as e:  # 连接失败不致命：降级为空工具集
            self._error = f"{type(e).__name__}: {e}"
            self._tools = []
            self._raw = []
        return self

    async def __aexit__(self, *exc: Any) -> None:
        for child in self._children:
            try:
                await child.__aexit__(*exc)
            except Exception:
                pass
        self._children = []
        if self._client is not None:
            try:
                await self._client.__aexit__(*exc)
            except Exception:
                pass
            self._client = None

    async def _discover(self) -> None:
        assert self._client is not None
        raw_tools = await self._client.list_tools()
        self._raw = raw_tools
        self._tools = [self._wrap(t) for t in raw_tools]

    def _wrap(self, t: Any) -> Tool:
        name = getattr(t, "name")
        description = getattr(t, "description", "") or name
        schema = getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
        client = self._client
        user_id = self._user_id

        async def _call(args: dict) -> str:
            assert client is not None
            payload = args if isinstance(args, dict) else {}
            if not user_id:
                return await _call_tool_safe(client, name, payload, schema)
            # Keep the semaphore scope user-wide: several sessions or agents for
            # the same user must share USER_MAX_MCP_CALLS.  Timeout is converted
            # into a normal tool observation so the model can continue.
            from .quota import QuotaExceeded, get_quota
            try:
                async with get_quota().mcp_call(user_id, timeout_s=2.0):
                    return await _call_tool_safe(client, name, payload, schema)
            except QuotaExceeded as exc:
                return f"[工具执行错误] {exc}"

        return Tool(name=name, description=description, input_schema=schema, call=_call)

    @property
    def tools(self) -> list[Tool]:
        return self._tools

    @property
    def error(self) -> Optional[str]:
        return self._error

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for t in self._raw:
            out.append({
                "name": getattr(t, "name", ""),
                "description": getattr(t, "description", "") or "",
                "input_schema": getattr(t, "inputSchema", None) or {},
            })
        return out

    def describe_raw(self) -> list[str]:
        return [getattr(t, "name", "") for t in self._raw]
