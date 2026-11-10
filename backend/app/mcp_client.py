"""MCP 客户端：把 FastMCP 工具转换成自研 Tool。

fastmcp.Client 直接接入，封装为自研 Tool（不再依赖 langchain）。
支持按「目标」构建：
- target 为 FastMCP 实例（内置 demo）-> in-process
- dict: {"transport": "http"/"sse", "url":..., "headers":...}
- dict: {"transport": "stdio", "command":...}
"""
from __future__ import annotations

import json
from dataclasses import replace
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


# 会话级失效的判定关键词。fastmcp 在底层传输断开后**不会自己重连**，错误一般是
# "Session terminated" / "Connection closed" / httpx ConnectError 等。
# 命中这些说明连接坏了（不是参数写错），必须重建 client 再重试，
# 否则模型无论怎么改参数都调不通（2026-11-10 实测：server 重启后永久 Session terminated）。
_SESSION_DEAD_MARKERS = (
    "session terminated",
    "connection closed",
    "connection reset",
    "server disconnected",
    "clientconnectorerror",
    "clientconnectionerror",
    "serverconnectionerror",
    "connecterror",
    "all connection attempts failed",
    "broken pipe",
    "transport closed",
    "stream closed",
    "no active session",
)


def _looks_session_dead(exc: Exception) -> bool:
    """异常是否表示 MCP 会话/传输已断（可通过重连恢复）。

    只认传输类关键词：McpError 也覆盖「工具自己抛的业务错误」，那类不该重连。
    """
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(m in msg for m in _SESSION_DEAD_MARKERS)


def _format_call_error(exc: Exception, schema: Any) -> str:
    """把工具异常渲染成模型可读的观察文本（参数纠错提示 + 原始信息）。"""
    msg = str(exc)
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
    return f"[工具执行错误] {type(exc).__name__}: {msg}{hint}"


def _apply_mcp_budget(user_id: str, server: str, tool_name: str, text: str) -> str:
    """MCP 结果也走统一预算层。

    为什么必须做：内置工具有 per-tool 上限 + 超限落盘，MCP 工具此前完全没有。
    实测 `run_readonly_sql` 一次返回 115 KB（1000 行），直接塞进 history 会
    吃掉整个上下文预算，紧接着触发压缩、甚至截断真正的回答。
    """
    from .tool_budget import apply_budget
    if not user_id:
        return apply_budget(f"mcp:{server}:{tool_name}", text)
    try:
        from .workspace import user_workspace
        where = user_workspace(user_id) / ".hlaudz" / "tool_results"
    except Exception:  # noqa: BLE001
        where = None
    return apply_budget(f"mcp:{server}:{tool_name}", text, persist_dir=where)


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

    def __init__(self, target: Any = None, *, user_id: str = "",
                 server_name: str = "") -> None:
        # target None -> 内置 demo
        self._target = target
        self._user_id = user_id
        self._server_name = (server_name or "").strip()
        self._client: Optional[Client] = None
        self._tools: list[Tool] = []
        self._raw: list[Any] = []
        self._error: Optional[str] = None
        # 每个已配置 server 的连接结果（成功/失败/工具数），由 build_user_toolset 填充。
        # 失败不能静默消失：否则模型只会看到「工具列表里没有」，然后编造原因。
        self._manifest: list[dict[str, Any]] = []
        # 合并时因跨 server 重名而被改名的工具（原名 -> 新名），供 manifest 如实披露。
        self._renamed: list[dict[str, str]] = []
        # 合并后：持有一组子 toolset，关闭时统一释放
        self._children: list["MCPToolset"] = []

    @classmethod
    def merge(cls, toolsets: list["MCPToolset"]) -> "MCPToolset":
        """聚合多个已连接的 toolset 的工具；关闭父级时统一释放全部子级。

        同名工具处理：不同 server 常常暴露同名工具（`health` / `search` / `list`）。
        registry 以 name 为唯一键，重复名会带来两个真实问题：
        1. 发给模型的 schema 里出现两个同名 function，模型分不清是哪一个；
        2. `dispatch()` 永远命中**最后**注册的那个，等于静默调错 server。

        做法：首个出现者保留原名，后续冲突者加 server 前缀（`<server>_<name>`）。
        工具名是 SSE / 前端 / 历史契约，所以只在真正冲突时改名；模型仍可从
        description 的 `[MCP: <server>]` 判断归属，改名记录也写进 manifest。
        """
        user_ids = {ts._user_id for ts in toolsets if ts._user_id}
        merged = cls(None, user_id=next(iter(user_ids)) if len(user_ids) == 1 else "")
        merged._children = list(toolsets)
        taken: set[str] = set()
        for ts in toolsets:
            for tool in ts.tools:
                final = tool
                if tool.name in taken:
                    prefix = (ts._server_name or "mcp").strip().replace(" ", "_")
                    candidate = f"{prefix}_{tool.name}"
                    n = 2
                    while candidate in taken:
                        candidate = f"{prefix}{n}_{tool.name}"
                        n += 1
                    final = replace(tool, name=candidate)
                    merged._renamed.append({
                        "server": ts._server_name or "mcp",
                        "original": tool.name,
                        "assigned": candidate,
                    })
                taken.add(final.name)
                merged._tools.append(final)
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

    async def reconnect(self) -> bool:
        """重建底层 client（会话断开后的自愈）。

        为什么需要：fastmcp 的 Client 在 server 重启 / 连接断开后不会自己恢复，
        缓存里的旧 client 会一直报 `Session terminated`。而 TTL 探测用的是**新**连接，
        看到的却是「已连接、12 个工具」，与缓存 manifest 一致 → 判定「状态没变」不重建，
        于是坏掉的 client 永远留在缓存里，用户每轮调用都失败，只能重启后端。
        重连成功后工具列表通常不变，模型侧无感知。
        """
        if self._target is None and not self._children:
            return False
        old = self._client
        try:
            self._client = Client(_build_target(self._target))
            await self._client.__aenter__()
            await self._discover()
            self._error = None
        except Exception as e:  # noqa: BLE001
            self._error = f"{type(e).__name__}: {e}"
            self._client = old
            return False
        if old is not None:
            try:
                await old.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        return True

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
        user_id = self._user_id
        toolset = self

        async def _invoke(payload: dict) -> str:
            """调用一次；会话失效时自动重连并重试一次。"""
            client = toolset._client
            if client is None:
                return f"[工具执行错误] MCP server {toolset._server_name or ''} 当前没有活动连接"
            try:
                result = await client.call_tool(name, payload)
                return _result_to_text(result)
            except Exception as exc:  # noqa: BLE001
                if not _looks_session_dead(exc):
                    return _format_call_error(exc, schema)
                # 连接坏了：重建 client 再试一次。server 重启/空闲超时是最常见原因，
                # 不重连的话模型只能一直看到 Session terminated。
                if not await toolset.reconnect():
                    return (
                        "[工具执行错误] MCP 会话已断开且自动重连失败"
                        f"（{type(exc).__name__}: {exc}）。"
                        f"该 server 当前不可用：{toolset._error or '重连失败'}"
                    )
                try:
                    result = await toolset._client.call_tool(name, payload)
                    return _result_to_text(result)
                except Exception as exc2:  # noqa: BLE001
                    return _format_call_error(exc2, schema)

        async def _call(args: dict) -> str:
            payload = args if isinstance(args, dict) else {}
            text: str
            if not user_id:
                text = await _invoke(payload)
            else:
                # Keep the semaphore scope user-wide: several sessions or agents for
                # the same user must share USER_MAX_MCP_CALLS.  Timeout is converted
                # into a normal tool observation so the model can continue.
                from .quota import QuotaExceeded, get_quota
                try:
                    async with get_quota().mcp_call(user_id, timeout_s=2.0):
                        text = await _invoke(payload)
                except QuotaExceeded as exc:
                    return f"[工具执行错误] {exc}"
            return _apply_mcp_budget(user_id, toolset._server_name, name, text)

        return Tool(name=name, description=description, input_schema=schema, call=_call,
                    source=self._source_label())

    def _source_label(self) -> str:
        if self._server_name:
            return f"MCP: {self._server_name}"
        if self._target is not None and not isinstance(self._target, dict):
            return "内置 demo MCP"
        return ""

    @property
    def tools(self) -> list[Tool]:
        return self._tools

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def manifest(self) -> list[dict[str, Any]]:
        """各 MCP server 的连接状态；合并后的 toolset 汇总全部子 server。"""
        if self._manifest:
            return self._manifest
        if self._children:
            return [c.status() for c in self._children]
        if self._target is None and not self._children:
            return []
        return [self.status()]

    def status(self) -> dict[str, Any]:
        """单个 MCP server 的连接状态，供系统提示词与设置页展示。"""
        return {
            "name": self._server_name or self._source_label() or "unknown",
            "ok": self._error is None,
            "tool_count": len(self._tools),
            "error": self._error or "",
            "tools": [t.name for t in self._tools],
        }

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for t in self._raw:
            out.append({
                "server": self._server_name,
                "name": getattr(t, "name", ""),
                "description": getattr(t, "description", "") or "",
                "input_schema": getattr(t, "inputSchema", None) or {},
            })
        return out

    def describe_raw(self) -> list[str]:
        return [getattr(t, "name", "") for t in self._raw]
