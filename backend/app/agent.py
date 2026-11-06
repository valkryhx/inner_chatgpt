"""Per-user agent runtime assembly (self-built kernel; no langchain/langgraph).

- Shared HistoryStore (single authoritative history per session).
- One UserRuntime per (user, selected model): LLMClient + ToolRegistry + AgentLoop,
  rebuilt when user LLM/MCP config changes (RuntimeHub.invalidate).
- Falls back to llm.yaml default when the user has no usable model.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from . import db
from .config import load_llm_settings
from .llm import build_client, build_client_from_config
from .mcp_client import MCPToolset
from .tools import ToolRegistry
from .agent_loop import AgentLoop
from .history_store import get_history_store
from .builtin_tools import build_builtin_tools
from .skill_store import build_skill_prompt
from .subagent.prompts import ROOT_AGENT_HINT

SYSTEM_PROMPT = """你是 Hlaudz，一个中文智能 Agent，运行在多用户 Web 平台中，可以调用工具完成任务。回答简洁、准确、直接；需要事实或计算时优先使用工具，不要臆测。

## 工具使用与自纠（重要）
- 工具的参数定义以系统实际提供的为准；不要凭记忆编造参数名。不确定就先调用能列信息/查结构的工具。
- 工具调用失败或返回错误是正常现象：先读懂错误信息，再决定下一步，不要直接放弃。
- 失败升级策略：第 1 次失败→读错误、理解原因并改对参数；仍失败→换等价工具或换思路；再失败→如实告诉用户并说明已尝试方案。禁止原样重复同一个失败调用。
- 多步任务边做边核对上一步结果，再继续下一步；不确定就用工具探测，而不是猜测。
- 能用一步确定性工具解决的，不要绕远；工具给出结构化结果时，以结果为准作答。
- 若某工具返回的是可直接渲染的结果（例如含 type 字段的图表规格），该结果已由前端展示；正文只做文字解读与结论，不要重复粘贴原始 JSON 或代码块。

## 本地工作区与 Skill
- 你有 file_read / file_write / file_patch / list_files / code_run 等本地工具，文件读写都被限制在你的个人 workspace 内。
- 需要计算、跑脚本或验证结果时优先用 code_run（Python 优先），不要凭空心算大数或假设代码能跑通。
- 系统提示若列出 [Available Skills]，当任务与某个 Skill 匹配时先调用 load_skill 加载其完整说明，再按说明执行。
- **输出产物路径红线**：所有生成的文件（图片/文档/数据）只能写在你的个人 workspace 内。严禁写入 `frontend/dist`、`frontend/src`、仓库源码、系统目录或其它用户目录；构建产物会被清空，且这条路径会破坏部署。
- **展示图片**：先把图片生成/保存在 workspace，然后在回复中用标准 Markdown `![说明](相对路径.png)`。不要用原生 `<img>`，不要用 `127.0.0.1`/`localhost` 绝对地址，也不要把文件复制到构建目录。"""


class BuiltModel:
    """Built LLM client + token thresholds."""

    def __init__(self, client: Any, soft_limit: int, context_window: int, source: str,
                 hard_limit: int | None = None, model_name: str = "") -> None:
        self.client = client
        self.soft_limit = soft_limit
        self.context_window = context_window
        self.source = source
        self.hard_limit = hard_limit if hard_limit is not None else int(context_window * 0.95)
        self.model_name = model_name


async def build_user_model(user_id: str, llm_config_id: str | None = None,
                           system_prompt: str = SYSTEM_PROMPT) -> BuiltModel:
    """Pick model: session's llm config -> user's default -> llm.yaml default."""
    def _usable(c: dict | None) -> bool:
        return bool(c and c.get("api_key") and c.get("model") and c.get("base_url"))

    cfg = None
    if llm_config_id:
        cfg = await db.get_llm_config(user_id, llm_config_id)
    if not _usable(cfg):
        cfg = await db.get_default_llm_config(user_id)
    if _usable(cfg):
        client = build_client_from_config(cfg, system_prompt=system_prompt)
        ctx = int(cfg.get("context_window", 128000))
        soft = cfg.get("auto_compact_tokens") or int(ctx * 0.90)
        hard = cfg.get("hard_limit_tokens") or int(ctx * 0.95)
        return BuiltModel(client, int(soft), ctx, source=f"user:{cfg['id']}",
                          hard_limit=int(hard), model_name=cfg.get("model", ""))
    s = load_llm_settings()
    client = build_client(s, system_prompt=system_prompt)
    return BuiltModel(client, s.soft_limit_tokens, s.context_window, source="default",
                      hard_limit=s.hard_limit_tokens, model_name=s.model)


async def build_user_toolset(user_id: str) -> MCPToolset:
    """Assemble per-user MCP toolset (multi-server aggregated); fall back to demo."""
    servers = [m for m in await db.list_mcp_servers(user_id) if m.get("enabled")]
    if not servers:
        from mcp_server.server import mcp as demo_mcp
        return await MCPToolset(demo_mcp, user_id=user_id).__aenter__()

    connect_timeout = float(os.environ.get("MCP_CONNECT_TIMEOUT", "12"))
    toolsets: list[MCPToolset] = []
    for srv in servers:
        ts = None
        try:
            if srv.get("transport") == "builtin_demo":
                from mcp_server.server import mcp as demo_mcp
                ts = MCPToolset(demo_mcp, user_id=user_id)
                await asyncio.wait_for(ts.__aenter__(), timeout=connect_timeout)
            else:
                target = {
                    "transport": srv.get("transport", "http"),
                    "url": srv.get("url", ""),
                    "command": srv.get("command", ""),
                    "args": srv.get("args", []) or [],
                    "headers": srv.get("headers", {}) or {},
                    "env": srv.get("env", {}) or {},
                }
                ts = MCPToolset(target, user_id=user_id)
                await asyncio.wait_for(ts.__aenter__(), timeout=connect_timeout)
            if ts.tools:
                toolsets.append(ts)
            else:
                await ts.__aexit__(None, None, None)
        except Exception:
            if ts is not None:
                try:
                    await ts.__aexit__(None, None, None)
                except Exception:
                    pass
            continue
    if not toolsets:
        from mcp_server.server import mcp as demo_mcp
        return await MCPToolset(demo_mcp, user_id=user_id).__aenter__()
    return MCPToolset.merge(toolsets)


class UserRuntime:
    def __init__(self) -> None:
        self.model: Optional[BuiltModel] = None
        self.toolset: Optional[MCPToolset] = None
        self.registry: Optional[ToolRegistry] = None
        self.loop: Optional[AgentLoop] = None
        self.store = get_history_store()
        self.user_id = ""
        self.soft_limit = 0
        self.hard_limit = 0

    async def build(self, user_id: str, llm_config_id: str | None = None) -> "UserRuntime":
        self.user_id = user_id
        # System prompt = base + per-user skill index (L1 listing only; bodies load on demand).
        try:
            skill_block = build_skill_prompt(user_id)
        except Exception:
            skill_block = ""
        # 子代理编排规则只进 root agent 的 system prompt；子代理自己走
        # subagent.prompts.build_child_system_prompt()，不会看到这段。
        system_prompt = SYSTEM_PROMPT + skill_block + "\n\n" + ROOT_AGENT_HINT
        self.model = await build_user_model(user_id, llm_config_id, system_prompt=system_prompt)
        self.toolset = await build_user_toolset(user_id)
        # Built-in tools first so MCP tools can never shadow file_read / load_skill etc.
        mcp_tools = self.toolset.tools if self.toolset else []
        builtin = build_builtin_tools(user_id)
        builtin_names = {t.name for t in builtin}
        safe_mcp = [t for t in mcp_tools if t.name not in builtin_names]
        self.registry = ToolRegistry(builtin + safe_mcp)
        self.soft_limit = int(os.environ.get("AGENT_SOFT_LIMIT", self.model.soft_limit))
        self.hard_limit = int(os.environ.get("AGENT_HARD_LIMIT", self.model.hard_limit))
        max_turns = int(os.environ.get("AGENT_MAX_TURNS", os.environ.get("AGENT_RECURSION_LIMIT", "300")))
        self.loop = AgentLoop(self.model.client, self.registry, system_prompt,
                              max_turns=max_turns, history_store=self.store, user_id=user_id)
        return self

    async def close(self) -> None:
        if self.toolset is not None:
            try:
                await self.toolset.__aexit__(None, None, None)
            except Exception:
                pass
            self.toolset = None
        if self.model is not None and getattr(self.model, "client", None) is not None:
            try:
                await self.model.client.aclose()
            except Exception:
                pass
        self.registry = None
        self.loop = None

    async def sanitize_pending_tool_calls(self, session_id: str) -> bool:
        """Repair dangling tool_calls at the tail of the persisted history."""
        if self.loop is None:
            return False
        try:
            if self.user_id:
                history = await self.store.get(self.user_id, session_id)
            else:
                history = await self.store.get(session_id)
        except Exception:
            return False
        changed = await self.loop.sanitize(history)
        if changed:
            try:
                if self.user_id:
                    await self.store.set(self.user_id, session_id, history)
                else:
                    await self.store.set(session_id, history)
            except Exception:
                pass
        return changed
