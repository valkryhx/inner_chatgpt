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

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from .. import team_store
from ..acp.config import load_acp_settings
from ..agent_loop import AgentLoop
from ..builtin_tools import build_builtin_tools
from ..scope import TeamScope
from ..team_host import TeamHost
from ..team_tools import build_lead_tools
from ..tool_policy import POLICY_FULL, filter_tools as filter_tool_policy, normalize as normalize_policy
from ..tools import ToolRegistry
from .capabilities import ORCHESTRATION_TOOLS
from .context import AgentContext
from .events import EventBuffer
from .host import SubagentHost
from .tools import build_subagent_tools

if TYPE_CHECKING:  # 只为类型标注引入，运行期按需延迟导入（见 build_turn_runtime）
    from ..acp.manager import AcpSessionManager

logger = logging.getLogger(__name__)

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
    # 本轮 generation id：插话队列的键需要它（见 app/steer_queue.py）。
    generation_id: str = ""
    # 本轮的 GA 会话集合；未启用 / 预检失败时为 None（spec §11.3）。
    acp: AcpSessionManager | None = None
    # 本轮的能力档位（A2A token 决定；网页端恒为 full）。
    tool_policy: str = POLICY_FULL


def _max_turns() -> int:
    try:
        return int(os.environ.get("AGENT_MAX_TURNS", os.environ.get("AGENT_RECURSION_LIMIT", DEFAULT_MAX_TURNS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TURNS


async def build_turn_runtime(base_runtime, ctx: AgentContext,
                             tool_policy: str = POLICY_FULL) -> TurnRuntime:
    """从被缓存的 base UserRuntime 派生一个「本轮视图」（含子代理能力）。

    ``tool_policy`` 由调用方身份决定（A2A token 的档位）；网页端不传 = full，
    行为与历史版本完全一致。降档时靠「registry 里没有该工具」兜底，不弹任何审批。
    """
    policy_level = normalize_policy(tool_policy)
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
    # 能力档位：先过滤内置工具（read_only / chat_only 在这里就被削掉）。
    base_builtin = filter_tool_policy(base_builtin, policy_level)

    # 1) 精确构造基础 registry：先 builtin（含正确 user 绑定），再非同名 MCP 工具，
    #    并在这一层就把编排工具排除（防止 MCP 服务器提供同名工具造成混淆）。
    builtin_names = {t.name for t in base_builtin}
    merged = ToolRegistry(list(base_builtin))
    if policy_level == POLICY_FULL and base_registry is not None:
        for t in base_registry.tools:
            if t.name in builtin_names:
                continue
            if t.name in ORCHESTRATION_TOOLS:
                continue
            merged.add(t)

    # 2) 本轮事件缓冲 + 子代理宿主 + 5 个编排工具
    buffer = EventBuffer()
    host = SubagentHost(team_ctx, parent_runtime=base_runtime, buffer=buffer)
    if policy_level == POLICY_FULL:
        for t in build_subagent_tools(host):
            merged.add(t)

    # 2b) Team-lite：每轮创建一个隔离的 Team，root 即 lead。
    # team_id 由服务端生成；模型只能操作本轮 scope 内的任务，不能传 team_id。
    team_scope = TeamScope.from_agent(team_ctx.scope())

    def _team_event(ev: dict[str, Any]) -> None:
        buffer.put({"type": "team_event", "event": ev})

    def _deliver(message: dict[str, Any]) -> dict[str, Any]:
        """Route a just-stored team message to whichever side is alive.

        Lead -> teammate goes to that child's pre-turn queue; teammate -> lead
        goes to the lead's notification queue.  Doing this here (rather than
        leaving it to the model to call ``list_messages``) is what makes
        collaboration a platform guarantee instead of a model habit.
        """
        recipient = str(message.get("recipient_agent_id") or "")
        sender = str(message.get("sender_agent_id") or "")
        if team_store.normalize_agent_id(recipient) == team_store.LEAD_AGENT_ID:
            if sender and team_store.normalize_agent_id(sender) != team_store.LEAD_AGENT_ID:
                host.push_team_message(message)
                return {"delivered": True, "name": team_store.LEAD_AGENT_ID}
            return {"delivered": False, "reason": "lead 给自己发的消息不需要投递"}
        return host.deliver_team_message(recipient, message)

    team_host = TeamHost(team_scope, event_sink=_team_event, deliver_sink=_deliver)

    async def _spawn_teammate(**kwargs: Any) -> dict[str, Any]:
        return await host.spawn(
            task_name=str(kwargs.get("task_name", "")),
            message=str(kwargs.get("message", "")),
            max_turns=int(kwargs.get("max_turns", 0) or 0),
            team_id=team_id,
            role="teammate",
        )

    async def _close_teammate(name: str) -> None:
        """撤销一次派活（绑定被拒时用），确保不留孤儿 agent 占着槽位。"""
        if not (name or "").strip():
            return
        try:
            await host.close(name)
        except Exception:  # noqa: BLE001 - 清理失败不应掩盖真实的拒绝原因
            pass

    if policy_level == POLICY_FULL:
        for t in build_lead_tools(team_host, spawn_fn=_spawn_teammate):
            merged.add(t)
        team_host._close_fn = _close_teammate

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

    # 2c) ACP / GA：只有 root 的 registry 注入 5 个 ga_* 工具；子代理与 teammate
    #     在别处装配自己的 registry，因此天然拿不到（spec §7.1）。
    #     预检是准入闸（spec §11.3）：bridge import 不通就**整组不注入**，
    #     绝不把半个会话交给模型。preflight 是同步的（一次 import，正常百毫秒级），
    #     放进线程避免阻塞事件循环。
    acp: AcpSessionManager | None = None
    acp_settings = load_acp_settings()
    if acp_settings.enabled and policy_level == POLICY_FULL:
        try:
            from ..acp.manager import AcpSessionManager
            from ..acp.tools import build_acp_tools

            manager = AcpSessionManager(ctx=team_ctx, buffer=buffer, settings=acp_settings)
            bridge = await asyncio.to_thread(manager.preflight)
            # 先整体构造再统一 add：中途抛异常不能留下「一半 ga_* 工具」。
            acp_tools = build_acp_tools(manager)
        except Exception as exc:  # noqa: BLE001 - GA 不可用不能拖垮本轮
            logger.warning(
                "ACP 不可用，本轮不注入 ga_* 工具：%s: %s", type(exc).__name__, exc
            )
        else:
            if bridge is None:
                # 自定义命令（不含 -m）没有可探测的模块：不启动 GA 就无法判定可用性，
                # 因此照常注入但必须显式告警，绝不能让 §11.3 的闸静默放行。
                # 只记可执行文件，不把整条命令行（可能含参数里的密钥）写进日志。
                logger.warning(
                    "ACP 预检闸已跳过：自定义命令无可探测模块，无法在不启动 GA 的前提下"
                    "判定可用性；5 个 ga_* 工具仍已注入，首个 ga_spawn 才会暴露启动错误。"
                    "可执行文件=%s",
                    acp_settings.command[0] if acp_settings.command else "",
                )
            for t in acp_tools:
                merged.add(t)
            acp = manager

    # 2d) 跨回合通知：上一个 generation 里完成但没被注入的子代理通知，本轮补上
    try:
        await host.load_pending_notifications()
    except Exception:  # noqa: BLE001 - 补载失败不能让本轮启动失败
        pass

    # 3) 独立的 AgentLoop：**必须挂主对话的 history_store**，
    #    否则主对话的 assistant/tool 消息不会落 session_history，下一轮上下文就丢了。
    #    （子代理用独立的侧链表，与这里无关。）
    # 记忆注入器：包在 _lead_injector **外面**（背景知识先于本轮新发生的事）。
    # 注意它读的是 team_ctx.user_id（闭包），不从请求参数反查 —— A2A / 子代理
    # 路径上反查会取到错误的主体（记忆方案 §2.3 第三条保证）。
    from ..memory_inject import MemoryInjector, wrap_with_memory
    _memory_injector = MemoryInjector(team_ctx.user_id)

    # 轮次收尾钩子（M4 后台记忆提取）。team 路径的 loop 是本轮新建的，
    # 直接在这里挂；SSE 层的 _install_memory_turn_end 见到 _memory_aware
    # 就不会重复挂（一个 loop 只能有一个钩子）。
    from ..memory_extract import make_turn_end_hook as _make_turn_end_hook

    class _TurnEndUR:
        """钩子只需要 user_id 与 client，不需要完整的 UserRuntime。"""

        def __init__(self, user_id: str, model: object) -> None:
            self.user_id = user_id
            self.model = model

    _turn_end_ur = _TurnEndUR(team_ctx.user_id, base_runtime.model)

    def _lead_injector():
        """Lead 的注入点：团队收件箱 + 子代理结束通知。

        收件箱从数据库「消费」（一次标记 consumed，不会重复注入），通知走内存队列。
        两者都在每轮模型调用前合并注入，这样 teammate 发来的消息不依赖模型主动
        轮询 —— 这正是真实 e2e 中消息「发了但没人看到」的根因。
        """
        async def _drain() -> list[dict[str, Any]]:
            injected: list[dict[str, Any]] = []
            # 用户中途插话优先于系统通知：用户的话是本回合要处理的新要求，
            # 通知是已经发生的事实。先给模型看指令，更符合「边跑边改需求」的直觉。
            try:
                from ..steer_queue import get_queue, wrap_user_steer
                q = get_queue(team_ctx.user_id, team_ctx.session_id, team_ctx.generation_id)
                for item in q.drain():
                    injected.append({"role": "user",
                                     "content": wrap_user_steer(item["text"])})
            except Exception:  # noqa: BLE001 - 队列读失败不能拖垮本轮
                pass
            try:
                for row in await team_store.consume_inbox(team_scope, limit=20):
                    sender = str(row.get("sender_agent_id") or "")
                    label = sender[4:] if sender.startswith("sub:") else (sender or "成员")
                    injected.append({
                        "role": "user",
                        "content": f"[来自 {label} 的团队消息] {row.get('content', '')}",
                    })
            except Exception:  # noqa: BLE001 - 收件箱读失败不能拖垮本轮
                pass
            injected.extend(host.drain_notifications())
            return injected
        return _drain()

    def _team_gate() -> str:
        """lead 想收工时问一句：团队还有没有没跑完的活？

        返回空串 = 可以收工。返回非空 = 还有活，并说明是什么，循环据此续跑。

        为什么必须有这道闸：模型很自然会在「等 reader」之后结束自己那一轮，
        而队友的结论还没回来。没有闸的话，结果只能躺在收件箱里，任务板永远
        停在 pending —— 真机 runId 111915-64a6 就是这样丢了整个结论。

        为什么必须在有活时返回原因而不是布尔：同一句话要既给循环看、又注进
        模型上下文当指令，写两遍容易漂移。
        """
        live = [n for n in host.registry.active_nodes() if not n.is_idle]
        if live:
            names = "、".join(n.name for n in live[:5])
            return f"还有 {len(live)} 个队友正在运行（{names}），结果尚未返回。"
        idle = [n for n in host.registry.active_nodes() if n.is_idle]
        if idle:
            names = "、".join(n.name for n in idle[:5])
            return (f"{names} 仍在待命（空闲但未结束），你可以继续用它干活，"
                    "或者用 close_agent 释放槽位。")
        return ""

    loop = AgentLoop(
        base_runtime.model.client,
        merged,
        base_runtime.loop.system_prompt if base_runtime.loop else "",
        max_turns=_max_turns(),
        history_store=base_runtime.store,
        user_id=team_ctx.user_id,
        # 记忆（最外层，先注入）-> 子代理通知 / team 收件箱 / 用户插话。
        # 必须用 wrap_with_memory 组合而不是直接赋值：直接赋值会把
        # _lead_injector 覆盖掉，团队消息与子代理通知就再也进不来了。
        message_injector=wrap_with_memory(_lead_injector, _memory_injector),
        # 收工前的团队闸：队友还在跑就别结束这一轮
        team_gate=_team_gate,
        # 轮次正常收尾时的后台记忆提取（每件独立，失败不影响本轮）
        on_turn_end=_make_turn_end_hook(_turn_end_ur),
    )
    # 标记：本注入器已经会 drain 用户插话队列。非 team 路径据此避免再装一个，
    # 否则同一条消息会被两个注入器各取一次（其中一次取到空，看起来没事，
    # 但队列语义会变得难以推理）。
    loop._steer_aware = True
    # 标记：记忆注入器已经装好了。SSE 层见到这个标记就**不再装第二个**，
    # 只把本轮的检索线索（用户说了什么）补进去 —— 否则两个 MemoryInjector
    # 各有自己的 _injected_slugs，同一批记忆会被注入两遍。
    #
    # 这里不用 rebuild_message_injector 的槽位机制：team 路径的 loop 是
    # **本轮新建**的（不是 RuntimeHub 缓存的），不存在跨代叠加问题，
    # 直接在构造时组合一次更直观。
    loop._memory_aware = True
    loop._memory_injector = _memory_injector
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
        generation_id=team_ctx.generation_id,
        acp=acp,
        tool_policy=policy_level,
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
