"""把 SubagentHost 挂到「主 agent 的一次生成」上，并让主 LLM 认得编排工具。

为什么需要「每轮独立 AgentLoop」：
  RuntimeHub 按 (user, model) 缓存 UserRuntime，整个 UserRuntime 跨会话、跨轮复用。
  而子代理宿主必须绑定「本次生成的 session/generation/message」，
  所以不能塞进被缓存的 UserRuntime。

做法：
  - 复用 UserRuntime 的 model.client / model tips（连接池、模型配置）
  - 每轮从 base runtime 派生一个「本轮 Runtime 视图」：
      registry = 精确名单（builtin + MCP，去掉编排工具）+ 本轮 5 个编排工具
      loop     = AgentLoop(同一个 client, 派生 registry, base system_prompt)
  - 事件协议与原来完全一致（text/thinking/tool/done），SSE 层不用改。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ..agent_loop import AgentLoop
from ..builtin_tools import build_builtin_tools
from ..tools import ToolRegistry
from .capabilities import ORCHESTRATION_TOOLS
from .context import AgentContext
from .events import EventBuffer
from .host import SubagentHost
from .tools import build_subagent_tools

DEFAULT_MAX_TURNS = 48


@dataclass
class TurnRuntime:
    loop: AgentLoop
    host: SubagentHost
    buffer: EventBuffer
    system_prompt: str
    model: object
    store: object


def _max_turns() -> int:
    try:
        return int(os.environ.get("AGENT_MAX_TURNS", os.environ.get("AGENT_RECURSION_LIMIT", DEFAULT_MAX_TURNS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TURNS


def build_turn_runtime(base_runtime, ctx: AgentContext) -> TurnRuntime:
    """从被缓存的 base UserRuntime 派生一个「本轮视图」（含子代理能力）。"""
    base_registry = base_runtime.registry
    base_builtin = build_builtin_tools(ctx.user_id)

    # 1) 精确构造基础 registry：先 builtin（含正确 user 绑定），再非同名 MCP 工具，
    #    并在这一层就把编排工具排除（防止 MCP 服务器提供同名工具造成混淆）。
    builtin_names = {t.name for t in base_builtin}
    merged = ToolRegistry(list(base_builtin))
    if base_registry is not None:
        for t in base_registry.tools:
            if t.name in builtin_names:
                continue
            if t.name in ORCHESTRATION_TOOLS:
                continue
            merged.add(t)

    # 2) 本轮事件缓冲 + 子代理宿主 + 5 个编排工具
    buffer = EventBuffer()
    host = SubagentHost(ctx, parent_runtime=base_runtime, buffer=buffer)
    for t in build_subagent_tools(host):
        merged.add(t)

    # 3) 独立的 AgentLoop：**必须挂主对话的 history_store**，
    #    否则主对话的 assistant/tool 消息不会落 session_history，下一轮上下文就丢了。
    #    （子代理用独立的侧链表，与这里无关。）
    loop = AgentLoop(
        base_runtime.model.client,
        merged,
        base_runtime.loop.system_prompt if base_runtime.loop else "",
        max_turns=_max_turns(),
        history_store=base_runtime.store,
    )
    return TurnRuntime(
        loop=loop,
        host=host,
        buffer=buffer,
        system_prompt=loop.system_prompt,
        model=base_runtime.model,
        store=base_runtime.store,
    )
