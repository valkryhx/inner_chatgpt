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
    status: str = "starting"      # starting|running|idle|completed|errored|stopped|closed
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
    # steer：父代理在子代理运行期间追加的指令（下一次模型调用前注入）
    pending_messages: list[dict] = field(default_factory=list)
    followup_hashes: set[str] = field(default_factory=set)
    # ---- 持久 worker 状态（P-A）----
    # 唤醒源：消息到达 / 任务可认领 / 关闭。idle 循环等它，避免忙轮询。
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    # 进入空闲的时刻（monotonic），用于 idle 超时
    idle_since: float = 0.0
    # 跑完多少轮（与 turns 不同：turns 是模型调用次数）
    rounds: int = 0
    # 为什么空闲：available（在等活）/ done（明确做完）/ blocked（被依赖挡住）
    idle_reason: str = ""
    # 本轮是否已经主动 send_message 给 lead。已发过就抑制 idle 帧，
    # 避免 lead 收到「结果」+「空闲」两条重复通知（CC m1448 zo 的做法）。
    sent_to_lead_this_round: bool = False
    # 已投递过的 message_id。投递可能被重试（重连、循环重跑、补投），
    # 同一条消息投两次会让队友把同一件事做两遍。
    delivered_ids: set[str] = field(default_factory=set)

    @property
    def is_terminal(self) -> bool:
        """生命周期是否已结束。

        只表示「这个 agent 不会再有下一轮」：关闭/停止/出错/自然结束。
        持久 worker 在 idle（等后续消息）时 **不是终态** —— 用 :attr:`is_idle`。
        """
        return self.status in TERMINAL_STATUSES

    @property
    def is_idle(self) -> bool:
        """本轮工作已完成、正在等后续任务（仍活着，可被唤醒）。

        这个区分是持久化的核心：``wait_agent(condition="all_terminal")`` 与
        ``read_agent_result`` 关心的是「它这轮干完了没」，而槽位回收、SSE 关闭
        关心的是「它还会不会有下一轮」。两件事不能共用一个判定。
        """
        return self.status == "idle"

    @property
    def has_pending_work(self) -> bool:
        """是否还有未处理的消息（已投递但这一轮还没开跑）。"""
        return bool(self.pending_messages)

    @property
    def is_settled(self) -> bool:
        """这轮工作已了结：终态，或「空闲且没有待处理消息」。

        后半句是关键：``send_message`` 投递后节点状态仍是 ``idle``，要等循环被
        唤醒才变 ``running``。如果只看状态，``wait(all_terminal)`` 会在"消息刚到、
        活还没开干"这一瞬间误判成已了结，提前返回给 lead 一个过期结论。
        """
        if self.is_terminal:
            return True
        return self.is_idle and not self.has_pending_work

    def drain_pending(self) -> list[dict]:
        """取出待注入的追加指令（同步，供 AgentLoop 的 fold 点调用）。"""
        if not self.pending_messages:
            return []
        out = self.pending_messages[:]
        self.pending_messages.clear()
        return out

    def notify_waiters(self) -> None:
        for ev in self.waiters:
            ev.set()
        self.waiters.clear()
        # 唤醒 idle 循环：状态变化（如被关闭）要立刻让它退出等待
        self.wake.set()

    def to_dict(self) -> dict:
        out = {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role,
            "status": self.status,
            "turns": self.turns,
            "result_chars": len(self.result or ""),
            "error": self.error,
        }
        # 持久 worker 需要把「还在等活」这件事说清楚，否则模型看到 idle
        # 不知道是「该取结果」还是「该关掉」。
        if self.rounds:
            out["rounds"] = self.rounds
        if self.is_idle:
            out["idle"] = True
            out["pending_messages"] = len(self.pending_messages)
        return out


class SubagentRegistry:
    def __init__(self, *, max_depth: int = 2, max_active: int = 4) -> None:
        self.max_depth = max(0, int(max_depth))
        self.max_active = max(1, int(max_active))
        self._nodes: dict[str, AgentNode] = {}
        # 名字钉住：用过一次的名字不再复用（防止旧消息投进新 agent）
        self._name_history: dict[str, int] = {}

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
        requested = validate_task_name(name)
        name = requested

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

        # 同名但旧实例已结束/已回收：自动加后缀，而不是复用旧名字
        if name in self._name_history:
            base = name
            suffix = self._name_history[base] + 1
            while (f"{base}_{suffix}" in self._name_history
                   or self.get(f"{base}_{suffix}") is not None):
                suffix += 1
            name = validate_task_name(f"{base}_{suffix}")

        if len(self.active_nodes()) >= self.max_active:
            self._evict_one_idle()
            if len(self.active_nodes()) >= self.max_active:
                raise SubagentError(
                    f"同时存活的子代理已达上限 {self.max_active}；请先 close_agent 释放槽位"
                )

        # A child that participates in a team is always a teammate, even when
        # the caller did not pass team_id explicitly (it inherits the parent's
        # team).  Passing parent_ctx.role here would make the child a "lead"
        # and break build_teammate_tools() in SubagentRuntime.
        effective_role = "teammate" if (team_id or parent_ctx.team_id) else role
        ctx = parent_ctx.child(
            name, team_id=team_id,
            role="teammate",
        )
        node = AgentNode(agent_id=ctx.agent_id, name=name, ctx=ctx, role=effective_role)
        node.requested_name = validate_task_name(requested or name)
        self._nodes[name] = node
        self._name_history[name] = self._name_history.get(name, 0) + 1
        return node

    def _evict_one_idle(self) -> None:
        """腾一个槽位：先淘汰最久空闲的活着的节点，再淘汰终态节点。

        A persistent teammate sits in ``idle`` instead of reaching a terminal
        state, so the old "only finished nodes are reclaimable" rule would let
        four idle workers permanently block the fifth spawn (``max_active``).
        Idle-first is also the fair order: an agent that has been waiting longest
        is the one least likely to be needed next.
        """
        idle = sorted(
            (n for n in self._nodes.values() if n.is_idle),
            key=lambda n: (n.idle_since or 0.0, self._nodes_order(n)),
        )
        if idle:
            victim = idle[0]
            # 唤醒并停掉被淘汰的节点，否则它的 idle 循环还挂在事件循环上。
            victim.stop.set()
            victim.wake.set()
            self._nodes.pop(victim.name, None)
            return
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
