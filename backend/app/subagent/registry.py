"""子代理注册表：内存树 + 深度/槽位/重名护栏。

对齐 GenericAgent registry.py 的四条护栏（设计文档 §2.5）：
1. max_depth      —— 防止「子代理再派生」无限套娃
2. max_active     —— 同时存活上限（每个都是真实 LLM 花费）
3. 重名冲突        —— 存活代理同名直接报错，不静默改名（防双份计费）
4. idle 淘汰       —— 槽位不够时先回收已完成的
注意：这里的计数是**每 session** 的，不是全局（多租户公平性，设计文档 §7.3）。
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .context import AgentContext
from .events import StopSignal

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
RESERVED_NAMES = frozenset({"root", "main", "team-lead", "user", "system"})

TERMINAL_STATUSES = frozenset({"completed", "errored", "stopped", "closed"})


class SubagentError(RuntimeError):
    """注册表层面的拒绝（名称/深度/槽位/冲突）。"""


def validate_task_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not NAME_RE.match(n):
        raise SubagentError("task_name 只能是小写字母/数字/下划线，以字母或数字开头，长度 1-32")
    if n in RESERVED_NAMES:
        raise SubagentError(f"task_name {n!r} 是保留名")
    return n


@dataclass
class AgentNode:
    agent_id: str                 # "sub:<name>"
    name: str
    ctx: AgentContext
    role: str
    status: str = "starting"      # starting|running|completed|errored|stopped|closed
    run_id: str = ""
    task: Optional[asyncio.Task] = None
    stop: StopSignal = field(default_factory=StopSignal)
    turns: int = 0
    result: str = ""
    error: str = ""
    waiters: list[asyncio.Event] = field(default_factory=list)
    # 事件游标：用于 wait_agent 的 since_event_seq 增量读取（内存环形缓冲，重启即失效）
    event_seq: int = 0
    event_log: list[dict] = field(default_factory=list)
    quota_lease: object | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def notify_waiters(self) -> None:
        for ev in self.waiters:
            ev.set()
        self.waiters.clear()

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role,
            "status": self.status,
            "turns": self.turns,
            "result_chars": len(self.result or ""),
            "error": self.error,
        }


class SubagentRegistry:
    def __init__(self, *, max_depth: int = 2, max_active: int = 4) -> None:
        self.max_depth = max(0, int(max_depth))
        self.max_active = max(1, int(max_active))
        self._nodes: dict[str, AgentNode] = {}

    # ---------- queries ----------
    def get(self, name_or_id: str) -> Optional[AgentNode]:
        key = (name_or_id or "").strip()
        if key.startswith("sub:"):
            key = key[4:]
        for node in self._nodes.values():
            if node.name == key.lower():
                return node
        return None

    def all_nodes(self) -> list[AgentNode]:
        return list(self._nodes.values())

    def active_nodes(self) -> list[AgentNode]:
        return [n for n in self._nodes.values() if not n.is_terminal]

    def by_run_id(self, run_id: str) -> Optional[AgentNode]:
        for n in self._nodes.values():
            if n.run_id == run_id:
                return n
        return None

    # ---------- mutation ----------
    def create_child(self, parent_ctx: AgentContext, name: str, *,
                     team_id: str = "", role: str = "general") -> AgentNode:
        """校验并注册一个子节点。冲突/越界时抛 SubagentError（不静默改名）。"""
        name = validate_task_name(name)

        if parent_ctx.depth + 1 > self.max_depth:
            raise SubagentError(
                f"超过子代理最大深度 {self.max_depth}（当前深度 {parent_ctx.depth}）"
            )

        existing = self.get(name)
        if existing is not None and not existing.is_terminal:
            raise SubagentError(
                f"子代理 {name!r} 仍在运行（{existing.status}）；"
                "请先 wait_agent + close_agent 或换一个 task_name"
            )

        if len(self.active_nodes()) >= self.max_active:
            self._evict_one_idle()
            if len(self.active_nodes()) >= self.max_active:
                raise SubagentError(
                    f"同时存活的子代理已达上限 {self.max_active}；请先 close_agent 释放槽位"
                )

        effective_role = "teammate" if team_id else role
        ctx = parent_ctx.child(
            name, team_id=team_id,
            role="teammate" if team_id else parent_ctx.role,
        )
        node = AgentNode(agent_id=ctx.agent_id, name=name, ctx=ctx, role=effective_role)
        self._nodes[name] = node
        return node

    def _evict_one_idle(self) -> None:
        """回收最早完成的节点（只从内存移除，DB 记录保留）。"""
        finished = sorted(
            (n for n in self._nodes.values() if n.is_terminal),
            key=lambda n: self._nodes_order(n),
        )
        if finished:
            self._nodes.pop(finished[0].name, None)

    def _nodes_order(self, node: AgentNode) -> int:
        return list(self._nodes.keys()).index(node.name) if node.name in self._nodes else 0

    def mark_terminal(self, node: AgentNode, status: str, *, result: str = "", error: str = "") -> None:
        node.status = status
        node.result = result
        node.error = error
        node.notify_waiters()

    def remove(self, name: str) -> bool:
        return self._nodes.pop((name or "").strip().lower(), None) is not None

    def all_terminal(self, nodes: list[AgentNode]) -> bool:
        return bool(nodes) and all(n.is_terminal for n in nodes)
