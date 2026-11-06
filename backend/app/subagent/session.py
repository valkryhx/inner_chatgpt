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
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from .. import team_store
from ..agent_loop import AgentLoop
from ..builtin_tools import build_builtin_tools
from ..scope import TeamScope
from ..team_host import TeamHost
from ..team_tools import build_lead_tools
from ..tools import ToolRegistry
from .capabilities import ORCHESTRATION_TOOLS
from .context import AgentContext
from .events import EventBuffer
from .host import SubagentHost
from .tools import build_subagent_tools

DEFAULT_MAX_TURNS = 300


@dataclass
class TurnRuntime:
    loop: AgentLoop
    host: SubagentHost
    buffer: EventBuffer
    system_prompt: str
    model: object
    store: object
    user_id: str = ""
    team: TeamHost | None = None
    team_id: str = ""


def _max_turns() -> int:
    try:
        return int(os.environ.get("AGENT_MAX_TURNS", os.environ.get("AGENT_RECURSION_LIMIT", DEFAULT_MAX_TURNS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TURNS


async def build_turn_runtime(base_runtime, ctx: AgentContext) -> TurnRuntime:
    """从被缓存的 base UserRuntime 派生一个「本轮视图」（含子代理能力）。"""
    # Team id is server-generated before any host/child is constructed.  The
    # same immutable context then flows through builtin tools, subagent
    # sidechains and teammate TeamHost instances.
    team_id = "team_" + uuid.uuid4().hex[:12]
    team_ctx = ctx.with_team(team_id, role="lead")
    base_registry = base_runtime.registry
    base_builtin = build_builtin_tools(
        team_ctx.user_id,
        session_id=team_ctx.session_id,
        generation_id=team_ctx.generation_id,
        team_id=team_ctx.team_id,
        agent_id=team_ctx.agent_id,
    )

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
    host = SubagentHost(team_ctx, parent_runtime=base_runtime, buffer=buffer)
    for t in build_subagent_tools(host):
        merged.add(t)

    # 2b) Team-lite：每轮创建一个隔离的 Team，root 即 lead。
    # team_id 由服务端生成；模型只能操作本轮 scope 内的任务，不能传 team_id。
    team_scope = TeamScope.from_agent(team_ctx.scope())

    def _team_event(ev: dict[str, Any]) -> None:
        buffer.put({"type": "team_event", "event": ev})

    team_host = TeamHost(team_scope, event_sink=_team_event)

    async def _spawn_teammate(**kwargs: Any) -> dict[str, Any]:
        return await host.spawn(
            task_name=str(kwargs.get("task_name", "")),
            message=str(kwargs.get("message", "")),
            max_turns=int(kwargs.get("max_turns", 0) or 0),
            team_id=team_id,
            role="teammate",
        )

    for t in build_lead_tools(team_host, spawn_fn=_spawn_teammate):
        merged.add(t)

    # Teammate has its own TeamHost instance.  Mirror durable events produced by
    # teammate tools into the lead's SSE queue whenever the lead invokes any team
    # or subagent tool.  This keeps the collaboration panel live without adding a
    # second background poller for every generation.
    watched = {
        "team_status", "task_create", "task_get", "task_list", "task_update",
        "send_message", "list_messages", "spawn_teammate",
        "wait_agent", "read_agent_result", "list_agents", "close_agent",
    }
    for tool in list(merged.tools):
        if tool.name not in watched:
            continue
        original = tool.call

        async def observed(args: dict, _original=original) -> str:
            try:
                return await _original(args)
            finally:
                if team_host is not None:
                    try:
                        await team_host.events_since(team_host._observed_seq)
                    except Exception:
                        pass

        tool.call = observed

    # 3) 独立的 AgentLoop：**必须挂主对话的 history_store**，
    #    否则主对话的 assistant/tool 消息不会落 session_history，下一轮上下文就丢了。
    #    （子代理用独立的侧链表，与这里无关。）
    loop = AgentLoop(
        base_runtime.model.client,
        merged,
        base_runtime.loop.system_prompt if base_runtime.loop else "",
        max_turns=_max_turns(),
        history_store=base_runtime.store,
        user_id=team_ctx.user_id,
    )
    return TurnRuntime(
        loop=loop,
        host=host,
        buffer=buffer,
        system_prompt=loop.system_prompt,
        model=base_runtime.model,
        store=base_runtime.store,
        user_id=team_ctx.user_id,
        team=team_host,
        team_id=team_id,
    )


async def prepare_turn_team(turn: TurnRuntime | None, ctx: AgentContext) -> None:
    """Create the durable team before the model starts its first tool call."""
    if turn is None or turn.team is None:
        return
    try:
        await turn.team.create("Agent Team")
    except Exception as exc:
        # Team is an optional collaboration surface.  If its SQLite lease or
        # quota fails, the ordinary subagent path must still work.  A failed
        # create may already have acquired the long-lived lifecycle lease, so
        # release it before dropping the host instead of leaking a user slot.
        try:
            await turn.team.release_lifecycle()
        except Exception:
            pass
        turn.team = None
        try:
            turn.buffer.put({
                "type": "team_event",
                "event": {
                    "type": "team_unavailable",
                    "team_id": turn.team_id,
                    "status": "failed",
                    "payload": {"error": f"{type(exc).__name__}: {exc}"},
                },
            })
        except Exception:
            pass
