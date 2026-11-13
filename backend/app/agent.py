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
from .prompts_legacy import SYSTEM_PROMPT_LEGACY

SYSTEM_PROMPT = """你是 Hlaudz，一个中文智能 Agent，运行在多用户 Web 平台中，可以调用工具完成任务。

## 身份与边界
- 你的实际权限以本轮提供的工具列表为准；列表里没有的能力就是现在没有，不要假装，也不要用别的工具手写替代。
- 你有真实副作用：写文件、执行代码、对外发送都难以回滚。不确定时先问用户，而不是先改。

## 怎么工作
- 任务没做完不要结束回合：自己排查、自己改、自己验证，推进到可用再汇报；只有确实需要用户决策时才停下来问。
- 探测优先：失败时先获取信息（错误文本、文件内容、命令输出），再决定重试还是换方案。
- 失败升级：第 1 次读错误并改对参数；第 2 次探测环境；第 3 次换方案或问用户。禁止没有新信息的重复调用。
- 先定位根因再动手，不要在症状上打补丁。

## 规划与推进
- 默认意图：除非用户明确只要方案、只在提问、或明确说先别动手，否则用户要的是可用的结果——直接做、直接验证。
- 多步任务先列出可验证的步骤；一次只把一件事当作进行中；中途理解变了就先更新计划。
- 简单任务不要套计划：一步能做完的事直接做。

## 验证纪律
- 声称「完成 / 修好 / 做好」之前必须有证据：命令输出、工具返回、文件内容、页面结果。不要凭推理说「应该没问题」。
- 先跑最贴近改动的最小验证，再逐步扩大范围。
- 没跑成或跑不动的验证要在汇报里明说，不要让用户以为已经验证过。
- 生成网页 / 图表 / 文档时，要实际渲染或打开一次再汇报；确实做不到就明说「没有实际看到结果」。
- 用户让你「审查 / 看看有没有问题」时，先按严重程度列发现，再给结论；没有问题就明说，并说明检查了哪些方面。

## 工具使用
- 参数定义以本轮工具列表为准，不要凭记忆编造参数名；不确定就先调用能给出结构信息的工具。
- 找文件用 file_glob，找内容用 file_grep，读文件用 file_read（支持 start / count / keyword）。
- 只有 shell 才能做的事才用 code_run：跑测试、装依赖、执行脚本、启动服务。不要用 code_run 写 find / grep / cat 的替代品——慢、易错、输出还会被截断。
- 搜索结果太多时先缩小 path 或加 glob 过滤，不要改用 code_run 写正则脚本绕开。
- 一轮里可以并行发多个互不依赖的检索；有依赖的必须串行，不要先猜后补。
- 工具调用失败是正常现象：读懂错误再改，不要原样重复同一个失败调用。
- 外部事实要带来源；无法证实就写「未证实」，不要编造数字、日期或引用原文。

## Skill
- 开工前先做一次技能扫描：把【任务类型】与 Available Skills 的描述对照，不等用户点名。常见对应：排查 bug / 报错 → 诊断类；设计新功能或方案 → 头脑风暴类；审查 → review 类；做网页 / 图表 / 幻灯片 / 文档 → 产出类。
- 只要某个 skill 与任务领域、流程或产物形态部分相关，就先 load_skill 读全文再动手；不要因为「大概知道怎么做」跳过。
- 用户用 /<skill-name> 点名时必须 load_skill，且只用列表里出现过的名字。
- 扫描后确实没有匹配就不用加载，不要为了显得守规矩乱加载。

## 本地工作区与产物
- 文件工具都被限制在你的个人 workspace 内；需要计算、跑脚本或验证结果时优先 code_run，不要凭空心算。
- 输出产物路径红线：所有生成的文件（图片 / 文档 / 数据）只能写在个人 workspace 内。严禁写入 frontend/dist、frontend/src、仓库源码、系统目录或其它用户目录；构建产物会被清空，且这条路径会破坏部署。
- 展示图片：先生成 / 保存在 workspace，再用标准 Markdown ![说明](相对路径.png) 引用。不要用原生 <img>，不要用 127.0.0.1 / localhost 绝对地址，也不要把文件复制到构建目录。
- 若某工具返回可直接渲染的结构化结果（例如含 type 字段的图表规格），前端已经展示；正文只做文字解读与结论，不要重复粘贴原始 JSON 或代码块。

## 沟通与交付
- 跟随用户的语言（默认中文）；先给结论，再给必要的过程与证据。
- 引用代码位置写 `文件路径:行号`；产物给可点击的绝对路径或相对链接，不要粘贴大段原文。
- 有自然的下一步就简短提一句，不要长篇总结。"""


_PROMPT_VARIANTS = {"new": SYSTEM_PROMPT, "legacy": SYSTEM_PROMPT_LEGACY}


def select_system_prompt() -> str:
    """基础提示词。

    HLAUDZ_PROMPT_VARIANT=legacy 可切回改造前的版本，**只用于提示词消融实验**
    （backend/tests/e2e/ablation.py 会分别起两个进程跑同一批任务）。生产默认 new。
    环境变量在调用时读取，不要在 import 时缓存。
    """
    variant = (os.environ.get("HLAUDZ_PROMPT_VARIANT") or "new").strip().lower()
    return _PROMPT_VARIANTS.get(variant, SYSTEM_PROMPT)


def format_mcp_manifest(manifest: list[dict[str, Any]] | None,
                        renamed: list[dict[str, str]] | None = None) -> str:
    """Render per-server MCP status into the system prompt.

    The model only ever saw a flattened tool-name list, so it could not answer
    "does server X have tools here" and fell back to guessing (2026-11-10: it
    called metric_search yet claimed sqlbot was not connected).  Give it the
    authoritative manifest instead.

    ``renamed`` explains cross-server name collisions: when two servers expose
    ``health``, the second becomes ``<server>_health`` (otherwise dispatch always
    hits the last one).  Without this note the model sees a tool name it never
    asked for and may treat it as a different tool.
    """
    if not manifest:
        return ""
    lines = ["", "", "## MCP servers connected this turn (authoritative)"]
    lines.append(
        "The following MCP servers are enabled for your account. "
        "Every MCP tool description is prefixed with its owner, e.g. [MCP: <server>]."
    )
    for m in manifest:
        name = m.get("name") or "unknown"
        count = int(m.get("tool_count") or 0)
        if m.get("ok"):
            names = ", ".join(m.get("tools") or [])
            lines.append("- %s: connected, %d tools (%s)" % (name, count, names))
        else:
            err = (m.get("error") or "unknown reason").strip()
            lines.append("- %s: **connection FAILED this turn**, 0 tools. Reason: %s" % (name, err))
    if renamed:
        pairs = "; ".join(
            "%s's `%s` is exposed as `%s`" % (r.get("server") or "a server",
                                              r.get("original") or "?",
                                              r.get("assigned") or "?")
            for r in renamed
        )
        lines.append("")
        lines.append(
            "Name collision note (two servers expose the same tool name, so one was "
            "renamed to keep them distinct): " + pairs + "."
        )
    lines.append("")
    lines.append(
        "When asked whether you have a server's tools, answer from this list: a listed "
        "server IS connected; missing tools can only mean that server failed to connect "
        "this turn. Do not hand-roll the protocol via code_run, and do not treat comments "
        "in old workspace scripts as current facts. If a tool call fails with a transport "
        "error (session terminated / connection closed), the platform reconnects and "
        "retries automatically — read the error and retry once instead of switching to "
        "code_run."
    )
    return chr(10).join(lines)


def compose_system_prompt(skill_block: str = "", mcp_block: str = "") -> str:
    """base prompt + 每用户 skill 索引（L1）+ 子代理编排提示。"""
    return select_system_prompt() + skill_block + mcp_block + "\n\n" + ROOT_AGENT_HINT


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
    """Assemble per-user MCP toolset (multi-server aggregated); fall back to demo.

    Every tool carries ``source="MCP: <server name>"`` so the model can tell which
    server a tool belongs to.  Connection failures are recorded in ``.manifest``
    instead of being silently dropped: the old silent fallback made the agent
    claim "that MCP has no tools" while the server was merely down.

    **并发连接**（2026-11-12）：多个 server 同时连，总耗时 = 最慢那个，
    而不是各个相加。实测 4 server（其中 1 个 stdio 起不来、吃满 12s 超时）：
    串行 32s → 并发后接近单个超时。

    并发安全性已核实：
    - 连接**不走**配额（`quota.mcp_call` 只在工具调用时用，见 `mcp_client.py:301`）；
    - `MCPToolset.__aenter__` / `_discover` 只读自身状态，无全局锁或共享可变状态；
    - 每个 server 各自独立 `MCPToolset`，失败互不影响（各自 try/except）。
    """
    servers = [m for m in await db.list_mcp_servers(user_id) if m.get("enabled")]
    if not servers:
        from mcp_server.server import mcp as demo_mcp
        return await MCPToolset(demo_mcp, user_id=user_id).__aenter__()

    connect_timeout = float(os.environ.get("MCP_CONNECT_TIMEOUT", "12"))
    # 并发上限：用户配 20 个 server 时不该一次拉起 20 个子进程。
    # 8 是折中——常见配置（2–6 个）完全并发，极端配置也不会把机器打满。
    try:
        max_parallel = max(1, int(os.environ.get("MCP_CONNECT_CONCURRENCY", "8")))
    except (TypeError, ValueError):
        max_parallel = 8
    gate = asyncio.Semaphore(max_parallel)

    async def _connect_one(srv: dict) -> tuple[str, MCPToolset | None, dict]:
        """连一个 server，返回 (name, 可用 toolset 或 None, manifest 行)。

        绝不抛异常：单个 server 失败只影响它自己那一行 manifest。
        """
        name = srv.get("name") or srv.get("id") or "mcp"
        ts: MCPToolset | None = None
        async with gate:
            try:
                if srv.get("transport") == "builtin_demo":
                    from mcp_server.server import mcp as demo_mcp
                    ts = MCPToolset(demo_mcp, user_id=user_id, server_name=name)
                else:
                    target = {
                        "transport": srv.get("transport", "http"),
                        "url": srv.get("url", ""),
                        "command": srv.get("command", ""),
                        "args": srv.get("args", []) or [],
                        "headers": srv.get("headers", {}) or {},
                        "env": srv.get("env", {}) or {},
                    }
                    ts = MCPToolset(target, user_id=user_id, server_name=name)
                await asyncio.wait_for(ts.__aenter__(), timeout=connect_timeout)
                if ts.tools:
                    return name, ts, ts.status()
                # 连上了但零工具 / 连接失败都在 ts.error 里
                row = {
                    "name": name, "ok": False, "tool_count": 0,
                    "error": ts.error or "该 server 没有暴露任何工具", "tools": [],
                }
                await ts.__aexit__(None, None, None)
                return name, None, row
            except Exception as e:  # noqa: BLE001
                if ts is not None:
                    try:
                        await ts.__aexit__(None, None, None)
                    except Exception:
                        pass
                return name, None, {
                    "name": name, "ok": False, "tool_count": 0,
                    "error": f"{type(e).__name__}: {e}", "tools": [],
                }

    results = await asyncio.gather(
        *(_connect_one(s) for s in servers), return_exceptions=True)

    # **按配置顺序**还原：gather 保留入参顺序，所以这里天然是有序的。
    # manifest 的顺序会被前端直接展示，乱序会让用户以为配置变了。
    toolsets: list[MCPToolset] = []
    manifest: list[dict[str, Any]] = []
    for srv, res in zip(servers, results):
        if isinstance(res, BaseException):
            # 理论上 _connect_one 不会抛，但 CancelledError 等 BaseException 会到这里。
            # 不能让它把整批连接结果吞掉（其余 server 可能已经连好了）。
            if isinstance(res, asyncio.CancelledError):
                # 真被取消：把已连的关掉再抛，避免留下孤儿连接/子进程
                for ts in toolsets:
                    try:
                        await ts.__aexit__(None, None, None)
                    except Exception:
                        pass
                raise res
            name = srv.get("name") or srv.get("id") or "mcp"
            manifest.append({"name": name, "ok": False, "tool_count": 0,
                             "error": f"{type(res).__name__}: {res}", "tools": []})
            continue
        _name, ts, row = res
        if ts is not None:
            toolsets.append(ts)
        manifest.append(row)

    if not toolsets:
        from mcp_server.server import mcp as demo_mcp
        fallback = await MCPToolset(demo_mcp, user_id=user_id).__aenter__()
        fallback._manifest = manifest
        return fallback
    merged = MCPToolset.merge(toolsets)
    merged._manifest = manifest
    return merged


async def probe_mcp_status(user_id: str) -> list[dict]:
    """现场重连全部已启用 MCP server，只返回状态清单。

    与 ``build_user_toolset`` 的区别：用完立刻关闭，不留常驻连接。
    供「重新检测连接」按钮和 runtime 软 TTL 刷新共用。
    """
    ts = None
    try:
        ts = await build_user_toolset(user_id)
        return getattr(ts, "manifest", []) or []
    except Exception as e:  # noqa: BLE001
        return [{"name": "mcp", "ok": False, "tool_count": 0,
                 "error": f"{type(e).__name__}: {e}", "tools": []}]
    finally:
        if ts is not None:
            try:
                await ts.__aexit__(None, None, None)
            except Exception:
                pass


def with_effort(runtime, effort: str):
    """按会话级思考等级派生一份 runtime 视图（不改缓存对象）。

    runtime 是 (user, model) 级缓存，直接改它的 client 会让 A 会话的等级污染 B 会话。
    这里只替换 model.client，其余（registry / store / 限额）共享。
    空覆盖直接返回原对象，调用方不用分支。
    """
    from .llm import clone_client_with_effort

    if not (effort or "").strip():
        return runtime
    if runtime is None or getattr(runtime, "model", None) is None:
        return runtime
    import copy as _copy

    view = _copy.copy(runtime)
    model = _copy.copy(runtime.model)
    model.client = clone_client_with_effort(runtime.model.client, effort)
    view.model = model
    return view


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
        # Build the toolset first so the prompt can state which MCP servers are live.
        self.toolset = await build_user_toolset(user_id)
        mcp_block = format_mcp_manifest(getattr(self.toolset, "manifest", None),
                                        getattr(self.toolset, "_renamed", None))
        system_prompt = compose_system_prompt(skill_block, mcp_block)
        self.model = await build_user_model(user_id, llm_config_id, system_prompt=system_prompt)
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
