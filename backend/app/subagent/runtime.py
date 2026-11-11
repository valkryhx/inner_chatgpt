"""一个子代理 = 一个独立 AgentLoop（进程内 asyncio，不 per-agent 起进程）。

复用既有内核：AgentLoop / ToolRegistry / LLMClient / builtin_tools。
本模块只负责「组装」，不改内核。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from ..agent_loop import AgentLoop
from ..builtin_tools import build_builtin_tools
from ..skill_store import build_skill_prompt
from ..team_host import TeamHost
from ..team_tools import build_teammate_tools
from ..scope import TeamScope
from ..tools import ToolRegistry
from .capabilities import build_capability_profile, build_child_registry
from .context import AgentContext
from .prompts import build_child_system_prompt


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        v = int(str(raw).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


@dataclass
class SubagentSpec:
    name: str
    message: str = ""
    fork_turns: str = "none"          # none | all | recent:N（v1 只实现 none/all/recent）
    max_turns: int = 0                # 0 -> 用环境默认


class SubagentRuntime:
    """组装一个子代理的 client / registry / loop / 初始 history。"""

    def __init__(self, ctx: AgentContext, *, parent_runtime, spec: SubagentSpec):
        self.ctx = ctx
        self.parent = parent_runtime
        self.spec = spec
        self.client = None
        self.registry: ToolRegistry | None = None
        self.loop: AgentLoop | None = None
        self.history: list[dict] = []
        self.profile = None
        self.max_turns = 0

    async def build(self) -> "SubagentRuntime":
        # 1) 子代理默认继承父模型（同一个 LLMClient 实例；不新建连接池）。
        self.client = self.parent.model.client

        # 2) 技能索引按 user_id 隔离（子代理用自己的 user_id，技能层自动隔离）
        try:
            skill_block = build_skill_prompt(self.ctx.user_id)
        except Exception:
            skill_block = ""
        self.system_prompt = build_child_system_prompt(skill_block=skill_block)

        # 3) 闭世界工具集：父 registry 按能力裁剪（编排工具在子代理里根本不存在）
        parent_registry = self.parent.registry
        available = parent_registry.names() if parent_registry else []
        self.profile = build_capability_profile(available_tools=available)
        child_tools_registry = build_child_registry(parent_registry, self.profile)

        # 4) 写类工具隔离到该 run 的产出目录，避免并发互相覆盖（设计文档 §7.2）
        write_root = f".subagents/{self.ctx.session_id}/{self.spec.name}"
        self.write_root = write_root
        own_builtin = build_builtin_tools(
            self.ctx.user_id,
            write_root=write_root,
            session_id=self.ctx.session_id,
            generation_id=self.ctx.generation_id,
            team_id=self.ctx.team_id,
            agent_id=self.ctx.agent_id,
        )
        # 用「带隔离根的 builtin」替换父 registry 里同名的 builtin 工具；
        # MCP 工具（非 builtin 名）保持父级裁剪结果。
        builtin_names = {t.name for t in own_builtin}
        merged = ToolRegistry([t for t in own_builtin if self.profile.allows(t.name)])
        for t in child_tools_registry.tools:
            if t.name not in builtin_names:
                merged.add(t)
        # 4b) teammate 只在 Team 作用域内获得受限协作工具。
        # task_create / spawn_teammate / team_status 永不进入 teammate registry。
        if self.ctx.team_id:
            # Defensive: the child may inherit team_id without being marked
            # "teammate" (older contexts / custom callers).  Build an explicit
            # teammate scope so build_teammate_tools() can never fail here.
            team_scope = TeamScope(
                user_id=self.ctx.user_id,
                session_id=self.ctx.session_id,
                generation_id=self.ctx.generation_id,
                team_id=self.ctx.team_id,
                agent_id=self.ctx.agent_id,
                role="teammate",
            )

            def _deliver(message: dict) -> dict:
                """A teammate's message to the lead reaches the lead's queue.

                The teammate cannot touch the lead's宿主 directly; the sink is a
                callable handed in by the parent host, so the lead's next model
                call sees the message without polling.
                """
                sink = getattr(self, "lead_delivery_sink", None)
                if sink is None:
                    return {"delivered": False, "reason": "父代理投递通道未就绪"}
                try:
                    return sink(message) or {"delivered": True}
                except Exception as exc:  # noqa: BLE001 - 入库成功即可，投递失败要如实返回
                    return {"delivered": False, "reason": f"{type(exc).__name__}: {exc}"}

            team_host = TeamHost(team_scope, deliver_sink=_deliver)
            self.team_host = team_host
            for tool in build_teammate_tools(team_host):
                merged.add(tool)
        self.registry = merged

        # 5) 初始 history：默认只有一条 user message（上下文隔离）
        self.history = self._initial_history()

        # 24 was a legacy hard-coded cap.  It silently truncated both
        # spawn_agent(max_turns=40/45) and the default teammate budget.  Keep
        # the same 300-turn ceiling as the root loop unless explicitly set.
        self.max_turns = self.spec.max_turns or _env_int(
            "SUBAGENT_MAX_TURNS", _env_int("AGENT_MAX_TURNS", 300)
        )
        self.loop = AgentLoop(self.client, self.registry, self.system_prompt,
                              max_turns=self.max_turns, history_store=None)
        return self

    def _initial_history(self) -> list[dict]:
        fork = (self.spec.fork_turns or "none").strip().lower()
        messages: list[dict] = []
        if fork not in ("none", "", "0"):
            messages.extend(self._fork_history(fork))
        messages.append({"role": "user", "content": self.spec.message})
        return messages

    def _fork_history(self, fork: str) -> list[dict]:
        """继承父对话（可选）。v1 支持 all / recent:N。"""
        try:
            parent_history = getattr(self.parent, "_fork_source_history", None) or []
        except Exception:
            parent_history = []
        if not parent_history:
            return []
        if fork == "all":
            keep = list(parent_history)
        elif fork.startswith("recent:"):
            try:
                n = max(1, int(fork.split(":", 1)[1]))
            except (ValueError, IndexError):
                n = 6
            keep = list(parent_history)[-n * 2:]
        else:
            keep = []
        # 只保留 user/assistant 纯文本，不继承 tool_calls / tool 结果（避免半截工具链）
        out: list[dict] = []
        for m in keep:
            role = m.get("role")
            if role in ("user", "assistant") and not m.get("tool_calls"):
                out.append({"role": role, "content": m.get("content", "")})
        return out
