"""MCP 客户端：把 FastMCP 工具转换成自研 Tool。

fastmcp.Client 直接接入，封装为自研 Tool（不再依赖 langchain）。
支持按「目标」构建：
- target 为 FastMCP 实例（内置 demo）-> in-process
- dict: {"transport": "http"/"sse", "url":..., "headers":...}
- dict: {"transport": "stdio", "command":...}
"""
from __future__ import annotations

import asyncio
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
    # ---- 「从没连上」也算（2026-11-19 加）----
    #
    # fastmcp 的 `Client.__aenter__` 失败后，client 对象留在「未连接」状态，
    # 之后每次 `call_tool` 都抛 `Client is not connected. Use the 'async with
    # client:' context manager first.`
    #
    # 这条**以前不在白名单里**，于是 `_looks_session_dead` 返回 False ->
    # 不重连 -> 走 `_format_call_error`（还对模型说「请严格按工具参数定义调用」，
    # 完全误导）。结果是这个 server 在整个进程生命周期内**永久不可用**，
    # 用户看到的就是「重试也一样失败、非偶发」。
    #
    # 实测现场（hx 用户的 exa）：3 个工具全部同样的错，重试 3 次全失败。
    #
    # 为什么归到「可重连」：恢复手段与「会话断了」完全相同 —— 重建 client。
    "client is not connected",
    "not connected",
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


# MCP 工具调用的**默认**超时。
#
# ## 为什么必须显式给（2026-11-19 多实例事故）
#
# SDK 的默认值是「无超时」，两头都是 None：
#
#     fastmcp.Client.call_tool(..., timeout: ... | None = None)
#     mcp.ClientSession.call_tool(..., read_timeout_seconds: ... | None = None)
#
# 于是 server 收到请求但不回包时（半死连接 / 中间设备吞包 / 服务端 hang），
# `await call_tool(...)` 会**永久挂住**，协程永不恢复，`tool_result` 永不发出。
# 实测现场：两个子代理都发出 `web_search_exa` 的 `tool_start` 后再无下文，
# 父代理连等 240s + 300s 两次都 timeout，而实例进程健康、`/api/health` 20ms
# 返回 —— 事件循环没事，是这一个 await 不返回。
#
# 同一份代码新建连接只要 0.9s，说明不是网络不通，而是「旧连接卡住 + 没有兜底」。
# 对照 `runtime.py` 的探活：那里明确 `asyncio.wait_for` 并把超时判为死连接，
# 说明设计上知道会卡，只是执行路径漏了这层。
#
# ## 为什么是 300s
#
# 工具调用里最长的是联网搜索/抓取，正常几秒到几十秒。300s 既远大于正常耗时
# （不误杀慢请求），又远小于 user 能容忍的「永远转圈」。可用环境变量覆盖。
_DEFAULT_MCP_CALL_TIMEOUT_S = 300.0
_MCP_CALL_TIMEOUT_ENV = "MCP_CALL_TIMEOUT_S"
# 兜底下限/上限：0 或负数等于没有超时，必须退回默认。
_MCP_CALL_TIMEOUT_MIN_S = 1.0
_MCP_CALL_TIMEOUT_MAX_S = 3600.0


def mcp_call_timeout_s() -> float:
    """单次 MCP 工具调用的超时秒数（`MCP_CALL_TIMEOUT_S` 可覆盖）。

    坏配置（非数字 / 0 / 负数 / 超范围）一律退回默认值：宁可超时保守，
    也不能因为一个环境变量写错就让工具彻底不可用或变回「无超时」。
    """
    import os

    raw = os.environ.get(_MCP_CALL_TIMEOUT_ENV)
    if raw is None or not str(raw).strip():
        return _DEFAULT_MCP_CALL_TIMEOUT_S
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_MCP_CALL_TIMEOUT_S
    if v < _MCP_CALL_TIMEOUT_MIN_S or v > _MCP_CALL_TIMEOUT_MAX_S:
        return _DEFAULT_MCP_CALL_TIMEOUT_S
    return v


# MCP 并发槽位的**排队**等待时长。
#
# ## 为什么原来硬编码 2 秒是个缺陷（2026-11-19 事故）
#
# `_call` 里写的是 `mcp_call(user_id, timeout_s=2.0)`：拿不到槽位就 2 秒放弃、
# 直接抛 `capacity_exceeded`。事故里两个子代理占满槽位，父代理想自己补搜，
# 连续两次都在 2 秒后被拒 —— **唯一的自救路径被掐掉了**
# （docs/20261119-子代理卡死事故分析-MCP调用无超时.md §4）。
#
# 语义上「槽位满」应该是**排队**，不是**失败**：一次 MCP 调用本身允许 300 秒
# （见 mcp_call_timeout_s），排队 2 秒就放弃完全没有道理。
#
# ## 为什么是 30 秒
#
# 要显著大于「一次正常的 MCP 调用耗时」（实测 exa 约 0.9s，慢的几十秒），
# 又明显小于用户能感知的「卡住」。真正想快速失败时用环境变量调小。
_DEFAULT_MCP_QUEUE_WAIT_S = 30.0
_MCP_QUEUE_WAIT_ENV = "MCP_CALL_QUEUE_WAIT_S"
_MCP_QUEUE_WAIT_MIN_S = 0.1
_MCP_QUEUE_WAIT_MAX_S = 600.0


def mcp_call_queue_wait_s() -> float:
    """拿不到 MCP 并发槽位时，最多排队等多久（`MCP_CALL_QUEUE_WAIT_S` 可覆盖）。

    坏配置一律退回默认：宁可等久一点，也不能因为环境变量写错就退回
    「2 秒即失败」那套把用户自己锁死的行为。
    """
    import os

    raw = os.environ.get(_MCP_QUEUE_WAIT_ENV)
    if raw is None or not str(raw).strip():
        return _DEFAULT_MCP_QUEUE_WAIT_S
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_MCP_QUEUE_WAIT_S
    if v < _MCP_QUEUE_WAIT_MIN_S or v > _MCP_QUEUE_WAIT_MAX_S:
        return _DEFAULT_MCP_QUEUE_WAIT_S
    return v


class _McpCallTimeout(Exception):
    """单次 MCP 工具调用超时（内部信号，不外泄给模型）。

    单独一个类型而不是直接往上抛 `asyncio.TimeoutError`：因为外层还有一层
    `except Exception` 会把它归到「业务错误」，那会把「server 无响应」
    误报成「参数不对」，误导模型改参数重试。
    """

    def __init__(self, seconds: float) -> None:
        super().__init__(f"mcp call timeout after {seconds}s")
        self.seconds = seconds


def _timeout_error_text(server: str, tool: str, seconds: float) -> str:
    """超时变成可读的 tool observation，让模型能继续而不是整轮挂死。"""
    where = f"（server={server}）" if server else ""
    return (
        f"[工具执行错误] MCP 工具 {tool} 调用超时{where}："
        f"超过 {seconds:.0f}s 没有返回。"
        "已放弃本次调用。该 server 可能已无响应，"
        "可以稍后重试，或换用其他数据源继续。"
    )


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


def _is_local_url(url: str) -> bool:
    """URL 是否指向本机（loopback）。

    ## 为什么必须单独判定（2026-11-19 真机复现）

    本机 Windows 开着系统代理（`ProxyEnable=1`, `ProxyServer=127.0.0.1:7897`），
    而 httpx 默认 `trust_env=True` —— 它会把 **127.0.0.1 的请求也送进代理**。
    代理连不上那个本地端口，就返回 `502 Bad Gateway`。

    于是「MCP 服务没起」被伪装成「MCP 服务返回 502」，把排查引向错误方向
    （去查服务日志，而服务根本没在跑）。实测对照：

    | 配置 | 连不存在的本地端口 |
    | --- | --- |
    | 走代理（默认） | `502 Bad Gateway` ← 假象 |
    | 直连 | `ConnectError: WinError 10061 目标计算机积极拒绝` ← 真相 |

    只判 loopback（不看私有网段）：`192.168.x` / `10.x` 可能是局域网里
    真正需要经代理访问的服务，误判会让它们断掉。
    """
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    # 整个 127.0.0.0/8 都是 loopback；`localhost` 与 `::1` 同理。
    if host in ("localhost", "::1"):
        return True
    if host.startswith("127."):
        # 必须是纯 IPv4：`127.0.0.1.nip.io` 这类域名前缀不算
        parts = host.split(".")
        return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    return False


# 本地地址绕过代理时使用的 httpx 参数。常量导出供测试断言。
_LOCAL_PROXY_BYPASS = {"trust_env": False}


def _http_client_kwargs(url: str) -> dict:
    """给某个 URL 决定 httpx 参数。

    本地地址关 `trust_env`（不读系统代理）；外网保持默认（需要代理的环境仍能走）。
    """
    return dict(_LOCAL_PROXY_BYPASS) if _is_local_url(url) else {}


def _httpx_factory_for(url: str):
    """造一个 `httpx_client_factory`，让 fastmcp 用指定的 httpx 参数。

    fastmcp 的 transport 只接受 `httpx_client_factory` 来定制底层 httpx，
    没有直接暴露 `trust_env`。返回 `None` 表示用 fastmcp 的默认行为。

    ## 为什么不用 `create_mcp_http_client`

    它只接受 `headers / timeout / auth`，**不接受 `trust_env`**
    （实测 `TypeError: create_mcp_http_client() got an unexpected keyword
    argument 'trust_env'`）。所以这里自己造 `AsyncClient`，并把 MCP 协议
    依赖的两个默认值照抄过来：

    - `follow_redirects=True` —— MCP 的服务端可能 30x 到真实端点
    - 默认超时 30s —— 不设的话 httpx 默认 5s，长 tool call 会被掐
    """
    if not _is_local_url(url):
        return None

    import httpx

    def _factory(*args, **kwargs) -> "httpx.AsyncClient":
        # 合并：调用方（fastmcp）传的优先，我们只补 trust_env。
        merged = {**kwargs, **dict(_LOCAL_PROXY_BYPASS)}
        merged.setdefault("follow_redirects", True)
        merged.setdefault("timeout", httpx.Timeout(30.0))
        return httpx.AsyncClient(*args, **merged)

    return _factory


def _build_target(target: Any) -> Any:
    """把高层描述转成 fastmcp.Client 接受的 transport。

    兼容标准 mcp.json 的字段：stdio 用 command/args/env；http/sse 用 url/headers。

    **本地地址会绕过系统代理** —— 见 `_is_local_url` 的说明。
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
    url = target.get("url", "") or ""
    factory = _httpx_factory_for(url)
    if transport == "sse":
        from fastmcp.client.transports import SSETransport
        return SSETransport(url=url, headers=target.get("headers") or None,
                            httpx_client_factory=factory)
    # 默认 http（streamable-http）
    from fastmcp.client.transports import StreamableHttpTransport
    return StreamableHttpTransport(url=url, headers=target.get("headers") or None,
                                   httpx_client_factory=factory)


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
            """调用一次；会话失效时自动重连并重试一次。

            ## 两处调用都必须包超时

            `_call_guarded` 是唯一的出口 —— 原来这里是两条**裸** `await
            client.call_tool(...)`，而 SDK 默认 `timeout=None`（无限等）。
            server 半死时整轮生成会永久挂住（2026-11-19 事故，见模块顶部常量的说明）。
            """
            async def _call_guarded(target_client: Any) -> str:
                """带超时地调一次；超时转成可读错误而不是往上抛。"""
                seconds = mcp_call_timeout_s()
                try:
                    result = await asyncio.wait_for(
                        target_client.call_tool(name, payload), timeout=seconds)
                except (asyncio.TimeoutError, TimeoutError):
                    raise _McpCallTimeout(seconds) from None
                return _result_to_text(result)

            client = toolset._client
            if client is None:
                return f"[工具执行错误] MCP server {toolset._server_name or ''} 当前没有活动连接"
            try:
                return await _call_guarded(client)
            except _McpCallTimeout as exc:
                return _timeout_error_text(toolset._server_name, name, exc.seconds)
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
                    return await _call_guarded(toolset._client)
                except _McpCallTimeout as exc2:
                    return _timeout_error_text(toolset._server_name, name, exc2.seconds)
                except Exception as exc2:  # noqa: BLE001
                    return _format_call_error(exc2, schema)

        async def _call(args: dict) -> str:
            payload = args if isinstance(args, dict) else {}
            text: str
            if not user_id:
                text = await _invoke(payload)
            else:
                # Keep the semaphore scope user-wide: several sessions or agents for
                # the same user must share USER_MAX_MCP_CALLS.
                #
                # 槽位满时**排队**而不是 2 秒就失败：原来硬编码 `timeout_s=2.0`
                # 在事故里掐掉了父代理的自救路径（见 mcp_call_queue_wait_s 的说明）。
                # 排队超时仍转成正常 tool observation，模型能继续。
                from .quota import QuotaExceeded, get_quota
                try:
                    async with get_quota().mcp_call(
                            user_id, timeout_s=mcp_call_queue_wait_s()):
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
