"""SubagentHost：一个会话一次生成内的子代理生命周期管理器。

职责（设计文档 §6.4）：
- spawn / wait / read_result / list_agents / close 五个编排动作
- 把子代理事件扇出到父会话的 EventBuffer（与主代理事件共用一条 SSE）
- 级联停止、墙钟超时、侧链持久化
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any, Optional

from . import store as sub_store
from .context import AgentContext
from .events import EventBuffer
from .registry import AgentNode, SubagentError, SubagentRegistry
from .runtime import SubagentRuntime, SubagentSpec
from ..quota import QuotaExceeded, get_quota
from ..scope import TeamScope

DEFAULT_WAIT_TIMEOUT = 60.0
MAX_READ_CHARS = 20000


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        v = float(str(raw).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        v = int(str(raw).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


class SubagentHost:
    """按 (user, session, message, generation) 绑定；一次生成一个实例。"""

    def __init__(self, ctx: AgentContext, *, parent_runtime, buffer: EventBuffer) -> None:
        self.ctx = ctx
        self.parent_runtime = parent_runtime
        self.buffer = buffer
        self.registry = SubagentRegistry(
            max_depth=_env_int("SUBAGENT_MAX_DEPTH", 2),
            max_active=_env_int("SUBAGENT_MAX_ACTIVE", 4),
        )
        self.timeout_s = _env_float("SUBAGENT_TIMEOUT_S", 300.0)
        self._change = asyncio.Event()
        self._closed = False

    # ---------------- spawn ----------------
    async def spawn(self, *, task_name: str, message: str,
                    fork_turns: str = "none", background: bool = True,
                    max_turns: int = 0, team_id: str = "",
                    role: str = "general") -> dict:
        if self._closed:
            raise SubagentError("该会话的子代理宿主已关闭")
        if not (message or "").strip():
            raise SubagentError("message 不能为空")

        node = self.registry.create_child(
            self.ctx, task_name, team_id=team_id, role=role
        )
        node.status = "starting"
        node.run_id = "run_" + uuid.uuid4().hex[:12]

        quota_scope = TeamScope(
            self.ctx.user_id, self.ctx.session_id, self.ctx.generation_id,
            team_id or self.ctx.team_id or f"nogroup:{self.ctx.generation_id}",
            self.ctx.agent_id,
        )
        try:
            node.quota_lease = await get_quota().subagent_lease(quota_scope, timeout_s=2.0)
        except QuotaExceeded as e:
            self.registry.remove(node.name)
            raise SubagentError(str(e)) from e

        await sub_store.create_run(
            run_id=node.run_id,
            ctx_user_id=self.ctx.user_id,
            session_id=self.ctx.session_id,
            message_id=self.ctx.message_id,
            parent_agent_id=self.ctx.agent_id,
            agent_id=node.agent_id,
            name=node.name,
            role=node.role,
            task_message=message,
            status="running",
            generation_id=self.ctx.generation_id,
            team_id=team_id or self.ctx.team_id,
        )

        spec = SubagentSpec(
            name=node.name, message=message,
            fork_turns=fork_turns or "none", max_turns=max_turns,
        )
        node.task = asyncio.create_task(self._run_child(node, spec))
        self._publish({
            "type": "subagent_start",
            "agent_id": node.agent_id,
            "name": node.name,
            "role": node.role,
            "task": message,
        })
        return {
            "agent_id": node.agent_id,
            "name": node.name,
            "role": node.role,
            "run_id": node.run_id,
            "status": "running",
            "note": "已派活。之后用 wait_agent 等待、read_agent_result 读取结果。",
        }

    async def _run_child(self, node: AgentNode, spec: SubagentSpec) -> None:
        last_seq = 0
        runtime = None
        started = time.monotonic()
        try:
            node.status = "running"
            self._touch()
            runtime = SubagentRuntime(node.ctx, parent_runtime=self.parent_runtime, spec=spec)
            await runtime.build()
            node.turns = 0

            async def _drive():
                async for ev in runtime.loop.run_stream(runtime.history, stop_event=node.stop.event):
                    et = ev.get("type")
                    if et == "done":
                        return
                    if et == "error":
                        raise SubagentError(str(ev.get("error") or "子代理执行错误"))
                    if et in ("text_delta", "thinking_delta", "tool_start", "tool_result"):
                        node.event_seq += 1
                        node.event_log.append({"seq": node.event_seq, "event": ev})
                        # 环形缓冲：每节点最多保留最近 2000 条事件
                        if len(node.event_log) > 2000:
                            del node.event_log[: len(node.event_log) - 2000]
                        self._publish({
                            "type": "subagent_event",
                            "agent_id": node.agent_id,
                            "event": ev,
                        })
                    # 增量持久化侧链（每轮工具/文本后）
                    try:
                        last_seq = await sub_store.append_sidechain(
                            node.run_id, runtime.history, start_seq=last_seq,
                            user_id=self.ctx.user_id, session_id=self.ctx.session_id,
                            generation_id=self.ctx.generation_id, team_id=self.ctx.team_id,
                        )
                    except Exception:
                        pass

            await asyncio.wait_for(_drive(), timeout=self.timeout_s)
            node.status = "stopped" if node.stop.is_set() else "completed"
            node.turns = _count_turns(runtime.history)
            node.result = _final_text(runtime.history)
        except asyncio.TimeoutError:
            node.status = "stopped"
            node.error = f"timeout: 超过 {int(self.timeout_s)}s"
            node.result = _final_text(runtime.history) if runtime is not None else ""
        except asyncio.CancelledError:
            node.status = "stopped"
            node.result = _final_text(runtime.history)
            raise
        except Exception as e:  # noqa: BLE001
            node.status = "errored"
            node.error = f"{type(e).__name__}: {e}"
            node.result = _final_text(runtime.history)
        finally:
            lease = getattr(node, "quota_lease", None)
            if lease is not None:
                lease.release()
                node.quota_lease = None
            try:
                last_seq = await sub_store.append_sidechain(
                    node.run_id, runtime.history, start_seq=last_seq,
                    user_id=self.ctx.user_id, session_id=self.ctx.session_id,
                    generation_id=self.ctx.generation_id, team_id=self.ctx.team_id,
                )
            except Exception:
                pass
            try:
                await sub_store.finish_run(
                    node.run_id, status=node.status,
                    result_text=node.result or "", turns=node.turns, error=node.error or "",
                    user_id=self.ctx.user_id, session_id=self.ctx.session_id,
                    generation_id=self.ctx.generation_id, team_id=self.ctx.team_id,
                )
            except Exception:
                pass
            self._publish({
                "type": "subagent_end",
                "agent_id": node.agent_id,
                "name": node.name,
                "status": node.status,
                "result": node.result or "",
                "turns": node.turns,
                "error": node.error or "",
            })
            self._touch()

    # ---------------- wait ----------------
    async def wait(self, targets: Optional[list[str]] = None, condition: str = "all_terminal",
                   timeout_s: float = DEFAULT_WAIT_TIMEOUT,
                   since_event_seq: int = 0) -> dict:
        nodes = self._resolve_targets(targets)
        if not nodes:
            return {"timed_out": False, "condition": condition, "changed_agents": [],
                    "remaining_targets": [], "result_refs": {},
                    "recommended_next_action": "spawn_agent",
                    "note": "没有可等待的子代理"}
        condition = (condition or "all_terminal").strip().lower()
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        timed_out = False
        while True:
            if self._condition_met(nodes, condition):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            self._change.clear()
            # 清空后再检查一次，避免「刚好在 clear 之前完成」的丢失唤醒
            if self._condition_met(nodes, condition):
                break
            try:
                await asyncio.wait_for(self._change.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                timed_out = True
                break
        return self._wait_result(nodes, condition, timed_out, since_event_seq)

    def _condition_met(self, nodes: list[AgentNode], condition: str) -> bool:
        if condition == "event":
            return any(n.status != "starting" for n in nodes)
        # all_terminal / result_available
        return all(n.is_terminal for n in nodes)

    def _wait_result(self, nodes: list[AgentNode], condition: str, timed_out: bool,
                     since_event_seq: int = 0) -> dict:
        remaining = [n.name for n in nodes if not n.is_terminal]
        refs = {n.name: n.run_id for n in nodes if n.is_terminal}
        # since_event_seq > 0 时，只返回该游标之后的新事件（增量观察，避免重复灌上下文）
        incremental: list[dict] = []
        max_seq = int(since_event_seq or 0)
        if since_event_seq and int(since_event_seq) > 0:
            for n in nodes:
                for rec in n.event_log:
                    if rec["seq"] > int(since_event_seq):
                        incremental.append({"agent": n.name, "seq": rec["seq"], "event": rec["event"]})
                        max_seq = max(max_seq, rec["seq"])
        else:
            for n in nodes:
                max_seq = max(max_seq, n.event_seq)
        if timed_out:
            action = "wait_agent" if remaining else "read_agent_result"
        elif remaining:
            action = "wait_agent"
        else:
            action = "read_agent_result" if refs else "wait_agent"
        return {
            "timed_out": timed_out,
            "condition": condition,
            "changed_agents": [n.to_dict() for n in nodes],
            "remaining_targets": remaining,
            "result_refs": refs,
            "recommended_next_action": action,
            "event_seq": max_seq,
            "events": incremental,
            "note": ("等待超时不是失败：子代理仍在运行，继续 wait_agent 即可"
                     if timed_out else ""),
        }

    # ---------------- read result ----------------
    async def read_result(self, target: str, max_chars: int = MAX_READ_CHARS) -> dict:
        node = self.registry.get(target)
        if node is None:
            return {"ok": False, "error": f"未知子代理: {target}"}
        if not node.is_terminal:
            return {"ok": False,
                    "error": f"子代理 {node.name} 仍在运行（{node.status}），请先 wait_agent",
                    "status": node.status}
        text = node.result or ""
        if not text:
            row = await sub_store.get_run(
                node.run_id, user_id=self.ctx.user_id, session_id=self.ctx.session_id,
                generation_id=self.ctx.generation_id, team_id=self.ctx.team_id,
            )
            text = (row or {}).get("result_text", "") or ""
        limit = max(100, min(int(max_chars or MAX_READ_CHARS), MAX_READ_CHARS * 5))
        truncated = len(text) > limit
        return {
            "ok": True,
            "agent_id": node.agent_id,
            "name": node.name,
            "role": node.role,
            "status": node.status,
            "turns": node.turns,
            "error": node.error,
            "result": text[:limit],
            "truncated": truncated,
            "chars": len(text),
        }

    # ---------------- list ----------------
    def list_agents(self, include_closed: bool = False) -> list[dict]:
        out = []
        for n in self.registry.all_nodes():
            if n.status == "closed" and not include_closed:
                continue
            out.append(n.to_dict())
        return out

    # ---------------- close ----------------
    async def close(self, target: str, cascade: bool = False) -> dict:
        node = self.registry.get(target)
        if node is None:
            return {"ok": False, "error": f"未知子代理: {target}"}
        closed: list[str] = []
        if cascade:
            for child in list(self.registry.all_nodes()):
                if child.ctx.parent_agent_id == node.agent_id and not child.is_terminal:
                    await self._stop_node(child)
                    closed.append(child.name)
        await self._stop_node(node)
        closed.append(node.name)
        node.status = "closed"
        node.notify_waiters()
        self.registry.remove(node.name)
        self._touch()
        return {"ok": True, "closed": closed, "status": "closed"}

    async def _stop_node(self, node: AgentNode) -> None:
        node.stop.set()
        task = node.task
        if task is not None and not task.done():
            for _ in range(20):  # 最多 2s 优雅等待
                if task.done():
                    break
                await asyncio.sleep(0.1)
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass

    # ---------------- lifecycle ----------------
    async def stop_all(self) -> None:
        """用户点「停止」：把本代所有子代理标记停止并取消。

        Cancellation must be followed by a short bounded join.  Merely calling
        task.cancel() leaves child tasks unwinding in the background; they can
        still hold quota leases / write sidechains after the parent generation
        is reported stopped.  Bounded wait keeps the stop endpoint responsive
        even if a tool ignores cancellation.
        """
        tasks = []
        for n in self.registry.active_nodes():
            n.stop.set()
            if n.task is not None and not n.task.done():
                n.task.cancel()
                tasks.append(n.task)
        self._touch()
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=1.5)
            except Exception:
                pass

    async def close_all(self) -> None:
        self._closed = True
        for n in list(self.registry.all_nodes()):
            if not n.is_terminal:
                await self._stop_node(n)
        self.registry._nodes.clear()

    # ---------------- helpers ----------------
    def _resolve_targets(self, targets: Optional[list[str]]) -> list[AgentNode]:
        if not targets:
            return self.registry.active_nodes()
        out = []
        for t in targets:
            n = self.registry.get(t)
            if n is not None:
                out.append(n)
        return out

    def _touch(self) -> None:
        self._change.set()

    def _publish(self, ev: dict[str, Any]) -> None:
        try:
            self.buffer.put(ev)
        except Exception:
            pass


def _count_turns(history: list[dict]) -> int:
    return sum(1 for m in history if m.get("role") == "assistant")


def _final_text(history: list[dict]) -> str:
    for m in reversed(history or []):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            return str(m["content"])
    return ""
