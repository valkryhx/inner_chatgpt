"""SubagentHost：一个会话一次生成内的子代理生命周期管理器。

职责（设计文档 §6.4）：
- spawn / wait / read_result / list_agents / close 五个编排动作
- 把子代理事件扇出到父会话的 EventBuffer（与主代理事件共用一条 SSE）
- 级联停止、墙钟超时、侧链持久化
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Optional

from . import store as sub_store
from .context import AgentContext
from .events import EventBuffer
from .registry import AgentNode, SubagentError, SubagentRegistry
from .runtime import SubagentRuntime, SubagentSpec
from ..logging_setup import team_event
from ..quota import QuotaExceeded, get_quota
from ..scope import TeamScope

logger = logging.getLogger(__name__)

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


# 算「有实质进展」的事件：文本增量太碎，不唤醒等待者，否则 wait 会
# 被每个 token 反复唤醒，把上下文刷成一段段无用的半句话。
_WAKE_EVENTS = frozenset({"tool_start", "tool_result", "message_injected"})

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
        self.timeout_s = _env_float("SUBAGENT_TIMEOUT_S", 1800.0)
        # 持久 worker：跑完一轮后进入 idle 等后续任务的秒数。
        # 0 = 关闭（跑完即结束，与历史行为一致）。这是刻意的默认值：
        # 持久化改变的是「队友跑完还在不在」，属于产品语义变化，必须显式开启，
        # 否则 refresh 后看到的状态、通知文案、SSE 关闭时机全部跟着变。
        self.idle_timeout_s = _env_float("TEAMMATE_IDLE_TIMEOUT_S", 0.0)
        self._change = asyncio.Event()
        # 全局事件游标：wait_agent 的 since_event_seq 用它做增量游标。
        # 不能用 node.event_seq —— 那是每个节点各自从 0 开始的计数器，
        # 多个子代理时同一个游标会串味。
        self._gseq = 0
        self._closed = False
        # 子代理完成通知：peek/ack 分离，供母代理在 fold 点注入
        self._notifications: list[dict] = []

    # ---------------- spawn ----------------
    async def spawn(self, *, task_name: str, message: str,
                    fork_turns: str = "none", background: bool = True,
                    max_turns: int = 0, team_id: str = "",
                    role: str = "general", timeout_s: float = 0.0) -> dict:
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
        node.task = asyncio.create_task(
            self._run_child(node, spec, timeout_s=float(timeout_s or 0) or None))
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

    async def _run_child(self, node: AgentNode, spec: SubagentSpec,
                        *, timeout_s: float | None = None) -> None:
        last_seq = 0
        runtime = None
        started = time.monotonic()
        try:
            node.status = "running"
            self._touch()
            runtime = SubagentRuntime(node.ctx, parent_runtime=self.parent_runtime, spec=spec)
            await runtime.build()
            # 让 teammate 的 send_message 能真的把消息推进父代理队列，
            # 而不是只写库等父代理自己想起来轮询。
            runtime.lead_delivery_sink = self.push_team_message
            # steer：每轮模型调用前取出父代理追加的指令
            runtime.loop.message_injector = node.drain_pending
            node.turns = 0

            async def _drive():
                async for ev in runtime.loop.run_stream(runtime.history, stop_event=node.stop.event):
                    et = ev.get("type")
                    if et == "done":
                        return
                    if et == "error":
                        raise SubagentError(str(ev.get("error") or "子代理执行错误"))
                    if et in ("text_delta", "thinking_delta", "tool_start", "tool_result", "message_injected"):
                        node.event_seq += 1
                        self._gseq += 1
                        node.event_log.append({"seq": self._gseq, "event": ev})
                        if et in _WAKE_EVENTS:
                            # 有实质进展就唤醒 wait：让它马上把控制权还给母代理
                            self._touch()
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

            effective = float(timeout_s or self.timeout_s)
            deadline = started + effective
            # 持久 worker：一轮结束不等于 agent 结束。跑完一轮先进 idle 等后续，
            # 只有 idle 超时 / stop / 关闭才进终态。
            while True:
                if node.stop.is_set():
                    # 尊重外部已经判定好的终态（drain 回收会把「干过活」记成 completed）
                    if not node.is_terminal:
                        node.status = "stopped"
                    break
                if time.monotonic() >= deadline:
                    node.status = "stopped"
                    node.error = f"timeout: 超过 {int(effective)}s"
                    break
                # 每轮开始前补投库里积压的收件箱（发送时对方恰好不在活体队列）
                await self._refill_inbox(node, runtime)
                round_budget = min(effective, max(0.1, deadline - time.monotonic()))
                try:
                    await asyncio.wait_for(_drive(), timeout=round_budget)
                except asyncio.TimeoutError:
                    node.status = "stopped"
                    node.error = f"timeout: 超过 {int(effective)}s"
                    break
                # 一轮结束。被 stop/close 打断时必须记 stopped 而不是 completed，
                # 否则「关闭一个正在跑的队友」会在库里留下成功记录。
                if node.stop.is_set():
                    if not node.is_terminal:
                        node.status = "stopped"
                    node.rounds += 1
                    node.turns = _count_turns(runtime.history)
                    node.result = _final_text(runtime.history)
                    await self._snapshot_run(node, runtime, last_seq)
                    break
                node.status = "running"
                node.rounds += 1
                node.turns = _count_turns(runtime.history)
                node.result = _final_text(runtime.history)
                await self._snapshot_run(node, runtime, last_seq)
                if self.idle_timeout_s <= 0:
                    # 持久化关闭：跑完这一轮就结束，保持历史语义。
                    node.status = "completed"
                    break
                # 这一轮干完了：进 idle 等后续任务
                node.status = "idle"
                node.idle_since = time.monotonic()
                node.idle_reason = "available"
                node.wake.clear()
                self._touch()
                self._publish({
                    "type": "subagent_status",
                    "agent_id": node.agent_id,
                    "name": node.name,
                    "status": "idle",
                })
                # 本轮已经 send_message 给 lead 的话，不再重复推 idle 帧（CC m1448）
                if not node.sent_to_lead_this_round:
                    self.push_idle_notification(node, idle_reason="available")
                node.sent_to_lead_this_round = False
                idle_budget = min(self.idle_timeout_s,
                                  max(0.0, deadline - time.monotonic()))
                team_event(
                    "agent_round_done", agent=node.name, agent_id=node.agent_id,
                    team_id=self.ctx.team_id, round=node.rounds, turns=node.turns,
                    result_chars=len(node.result or ""),
                    idle_budget_s=round(idle_budget, 1))
                reason = await self._idle_wait(node, timeout_s=idle_budget)
                team_event("agent_idle_wait", agent=node.name,
                           team_id=self.ctx.team_id,
                           round=node.rounds, reason=reason,
                           pending=len(node.pending_messages))
                if reason == "stopped":
                    if not node.is_terminal:
                        node.status = "stopped" if node.stop.is_set() else "completed"
                    break
                if reason == "timeout":
                    # 空闲到期：没有新活就正常结束（这是持久 worker 的退出条件）
                    node.status = "completed"
                    break
                # 被唤醒：有新消息或新任务，回到 running 继续下一轮
                node.status = "running"
                self._publish({
                    "type": "subagent_status",
                    "agent_id": node.agent_id,
                    "name": node.name,
                    "status": "running",
                })
        except asyncio.TimeoutError:
            node.status = "stopped"
            node.error = f"timeout: 超过 {int(timeout_s or self.timeout_s)}s"
            node.result = _final_text(runtime.history) if runtime is not None else ""
        except asyncio.CancelledError:
            # 取消可能来自两条路：用户停止（应记 stopped），或本轮结束时的整体
            # 回收（drain）。后者已经把这个 agent 判成 completed —— 覆盖成 stopped
            # 会让「它明明干完过几轮」这个事实在库里消失。
            if not node.is_terminal:
                node.status = "stopped"
            node.turns = _count_turns(runtime.history) if runtime is not None else 0
            node.result = _final_text(runtime.history)
            raise
        except Exception as e:  # noqa: BLE001
            node.status = "errored"
            node.error = f"{type(e).__name__}: {e}"
            node.result = _final_text(runtime.history) if runtime is not None else ""
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
            await self._notify(node)
            team_event("agent_end", agent=node.name, agent_id=node.agent_id,
                       team_id=self.ctx.team_id,
                       status=node.status, rounds=node.rounds, turns=node.turns,
                       error=node.error or "")
            # 异常结束的队友占着的任务必须释放，否则任务板会烂在死 agent 手里：
            # 真机 runId 121743-7329 里两个 worker 因上游持续 502 而 errored，
            # 它们的任务永远停在 in_progress，第三个任务永远等不到依赖完成。
            # 团队韧性要求「一个 agent 出问题不能拖垮整个团队」——这里由平台
            # 兜底，不依赖模型记得手动 reclaim。
            if node.status in ("errored", "stopped") and runtime is not None:
                await self._release_dead_agent_tasks(node, runtime)
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

    @staticmethod
    def _sender_label(row: dict[str, Any]) -> str:
        sender = str(row.get("sender_agent_id") or row.get("sender") or "lead")
        return sender[4:] if sender.startswith("sub:") else sender

    async def _release_dead_agent_tasks(self, node: AgentNode, runtime) -> None:
        """把「死掉的队友」还占着的任务放回 pending，让 lead 能重新派活。

        只处理 owner 就是它、且还是 ``in_progress`` 的任务。用 CAS 更新，
        拿不到就跳过——绝不能把别人刚接手的任务抢回来。
        """
        team_host = getattr(runtime, "team_host", None)
        if team_host is None:
            return
        try:
            tasks = await team_host.list_tasks()
        except Exception:  # noqa: BLE001 - 释放失败不能影响收尾
            return
        released: list[str] = []
        for row in tasks or []:
            if (str(row.get("owner_agent_id") or "") != node.agent_id
                    or str(row.get("status") or "") != "in_progress"):
                continue
            try:
                out = await team_host.update_task(
                    str(row.get("task_id") or ""), "reclaim",
                    expected_version=int(row.get("version") or 0))
            except Exception:  # noqa: BLE001
                continue
            if out.get("ok"):
                released.append(str(row.get("task_id") or ""))
        if released:
            team_event("task_released_on_failure", agent=node.name,
                       agent_id=node.agent_id, team_id=self.ctx.team_id,
                       status=node.status, task_ids=released[:10])

    async def _refill_inbox(self, node: AgentNode, runtime) -> int:
        """把库里积压的团队消息补进待注入队列。

        发送时对方恰好不在活体队列（例如刚进 idle、或消息在它上一轮结束后才发）时
        只能靠这一步补投，否则那条消息会一直停在 ``queued``。
        """
        team_host = getattr(runtime, "team_host", None)
        if team_host is None:
            return 0
        try:
            rows = await team_host.consume_inbox(limit=20)
        except Exception:  # noqa: BLE001 - 补投失败不能拖垮子代理
            return 0
        for row in rows:
            node.pending_messages.append({
                "role": "user",
                "content": f"[来自 {self._sender_label(row)} 的团队消息] "
                           f"{row.get('content', '')}",
            })
        return len(rows)

    async def _snapshot_run(self, node: AgentNode, runtime, last_seq: int) -> None:
        """把本轮进度写进 DB，但**不**把 run 标成终态。

        持久 worker 一个 run_id 跑多轮；只有 finish_run 会写终态，中间快照用
        update_run_progress，这样刷新页面看到的是最新一轮而不是第一轮。
        """
        try:
            await sub_store.append_sidechain(
                node.run_id, runtime.history, start_seq=last_seq,
                user_id=self.ctx.user_id, session_id=self.ctx.session_id,
                generation_id=self.ctx.generation_id, team_id=self.ctx.team_id,
            )
        except Exception:
            pass
        try:
            await sub_store.update_run_progress(
                node.run_id, turns=node.turns, result_text=node.result or "",
                rounds=node.rounds,
                user_id=self.ctx.user_id, session_id=self.ctx.session_id,
                generation_id=self.ctx.generation_id, team_id=self.ctx.team_id,
            )
        except Exception:
            pass

    # ---------------- team message delivery ----------------
    def deliver_team_message(self, target: str, message: dict[str, Any]) -> dict[str, Any]:
        """Push one team message into a live agent's pre-turn queue.

        This is the hard-delivery path: a message written by ``send_message`` is
        handed to the agent's own loop, which injects it before its next model
        call. Without it, "cooperation" degenerates into the model remembering to
        call ``list_messages`` — which real runs showed it often does not.

        Address resolution accepts both ``sub:<name>`` and a bare name, because
        the lead writes the name it saw in ``spawn_teammate``'s reply.
        """
        name = str(target or "").strip()
        if name.startswith("sub:"):
            name = name[4:]
        name = name.replace(chr(92), "/").rstrip("/").split("/")[-1].lower()
        if not name:
            return {"delivered": False, "reason": "收件人为空"}

        node = self.registry.get(name)
        if node is None:
            team_event("message_undelivered", agent=name,
                       team_id=self.ctx.team_id,
                       reason="no_live_agent", delivered=False)
            return {"delivered": False,
                    "reason": f"没有名为 {name} 的活着的 agent（可能还未 spawn 或已被 close）"}
        if node.is_terminal:
            return {"delivered": False,
                    "reason": f"agent {name} 已结束（{node.status}），"
                              "消息已入库；需要它继续工作请重新 spawn_agent"}

        sender = str(message.get("sender_agent_id") or message.get("sender") or "lead")
        sender_label = sender[4:] if sender.startswith("sub:") else sender
        content = str(message.get("content") or "")
        # 幂等：同一条消息（按 message_id）只能入队一次。投递会被重试——重连、
        # 循环重跑、_refill_inbox 补投都可能再走一遍，重复入队会让队友把同一件事做两遍。
        mid = str(message.get("message_id") or "")
        if mid:
            if mid in node.delivered_ids:
                team_event("message_duplicate", agent=name,
                           team_id=self.ctx.team_id, message_id=mid)
                return {"delivered": True, "name": name, "agent_id": node.agent_id,
                        "duplicate": True, "idle": node.is_idle}
            if len(node.delivered_ids) > 500:
                node.delivered_ids.clear()
            node.delivered_ids.add(mid)
        node.pending_messages.append({
            "role": "user",
            "content": f"[来自 {sender_label} 的团队消息] {content}",
        })
        # 唤醒 idle 的队友：否则它睡到超时才发现有新消息。
        node.wake.set()
        self._touch()
        team_event("message_delivered", agent=name, message_id=mid or "",
                   team_id=self.ctx.team_id,
                   sender=sender_label, was_idle=node.is_idle,
                   pending=len(node.pending_messages))
        return {"delivered": True, "name": name, "agent_id": node.agent_id,
                "idle": node.is_idle, "logged": True,
                "pending": len(node.pending_messages)}

    def push_team_message(self, message: dict[str, Any]) -> None:
        """Queue a teammate->lead message for the lead's next turn.

        The lead's "inbox" is the notification queue the main loop already drains
        before every model call, so reusing it keeps one injection point instead
        of adding a second poller that could disagree about ordering.
        """
        sender = str(message.get("sender_agent_id") or message.get("sender") or "")
        sender_label = sender[4:] if sender.startswith("sub:") else (sender or " teammate")
        content = str(message.get("content") or "")
        kind = str(message.get("kind") or "note")
        item = {
            "id": uuid.uuid4().hex[:16],
            "agent": sender_label,
            "status": f"消息（{kind}）",
            "result_chars": len(content),
            "preview": content[:400],
            "is_message": True,
        }
        self._notifications.append(item)
        # 标记「本轮已主动汇报过」，让后面的 idle 帧去重（CC m1448 zo）。
        node = self.registry.get(sender_label)
        if node is not None:
            node.sent_to_lead_this_round = True
        self._touch()

    async def deliver_pending_inbox(self, team_host) -> int:
        """Drain this generation's durable inbox into the live queue, once per turn.

        Used at turn start so a message sent while the agent was between turns is
        not lost: the sender could not push it (no live task at that instant), but
        the row is still ``queued`` in the database.
        """
        try:
            rows = await team_host.consume_inbox(limit=20)
        except Exception:  # noqa: BLE001 - a broken inbox must not fail the turn
            return 0
        for row in rows:
            self.push_team_message(row)
        return len(rows)


    async def _notify(self, node: "AgentNode") -> None:
        item = {
            "id": uuid.uuid4().hex[:16],
            "agent": node.name,
            "status": node.status,
            "result_chars": len(node.result or ""),
            "preview": (node.result or "")[:400],
        }
        self._notifications.append(item)
        try:
            await sub_store.add_notification(
                nid=item["id"],
                user_id=self.ctx.user_id,
                session_id=self.ctx.session_id,
                generation_id=self.ctx.generation_id,
                agent_name=item["agent"],
                status=item["status"],
                preview=item["preview"],
            )
        except Exception:  # noqa: BLE001 - 通知落库失败不能影响子代理收尾
            pass

    async def load_pending_notifications(self, limit: int = 20) -> int:
        """从 DB 补载本会话未读通知（跨 generation 的新 host 也能拿到）。"""
        try:
            rows = await sub_store.list_pending_notifications(
                self.ctx.user_id, self.ctx.session_id, limit
            )
        except Exception:  # noqa: BLE001
            return 0
        known = {n["id"] for n in self._notifications}
        added = 0
        for r in rows:
            if r["id"] in known:
                continue
            self._notifications.append({
                "id": r["id"],
                "agent": r["agent_name"],
                "status": r["status"],
                "result_chars": len(r.get("preview") or ""),
                "preview": r.get("preview") or "",
            })
            added += 1
        return added

    def peek_notifications(self, limit: int = 5) -> list[dict]:
        return self._notifications[: max(1, int(limit))]

    def ack_notifications(self, ids) -> None:
        drop = set(ids or [])
        if not drop:
            return
        self._notifications = [n for n in self._notifications if n["id"] not in drop]

    def drain_notifications(self, limit: int = 5) -> list[dict]:
        """取一批通知并 ack，返回一条可直接 append 的 user 消息。

        Idle notifications and end notifications are batched under different
        headers: an idle teammate has *not* ended, and telling the model
        "已结束" would make it re-spawn a worker that is still standing by.
        """
        items = self.peek_notifications(limit)
        if not items:
            return []
        ids = [n["id"] for n in items]
        self.ack_notifications(ids)
        try:
            asyncio.get_running_loop().create_task(sub_store.ack_notifications_db(ids))
        except Exception:  # noqa: BLE001
            pass
        idle_items = [n for n in items if n.get("is_idle")]
        done_items = [n for n in items if not n.get("is_idle")]
        extra = len(self._notifications)

        chunks: list[str] = []
        if done_items:
            lines = [f"- {n['agent']} 已{n['status']}：{n['preview']}" for n in done_items]
            chunks.append("[任务通知] 以下子代理已经结束，结果可直接使用；"
                          "需要全文请 read_agent_result。\n" + "\n".join(lines))
        if idle_items:
            lines = []
            for n in idle_items:
                reason = n.get("idle_reason") or "available"
                lines.append(f"- {n['agent']} 已完成本轮、当前空闲（{reason}）：{n['preview']}")
            chunks.append("[空闲通知] 以下队友**没有结束**，只是在等下一步；"
                          "给它新任务请用 send_message，不要重新 spawn。\n"
                          + "\n".join(lines))
        if extra:
            chunks.append(f"（另有 {extra} 条通知待处理，可用 list_agents 查看）")
        return [{"role": "user", "content": "\n\n".join(chunks)}]

    # ---------------- steer ----------------
    async def followup(self, target: str, message: str, max_per_agent: int = 5) -> dict:
        """向运行中的子代理追加一条指令；在下一次模型调用前注入。

        The limit is on **unedelivered** messages, not on the lifetime total: a
        persistent teammate can legitimately receive more than five instructions
        across its lifetime, and refusing the sixth would strand a worker that is
        still willing to work.  The de-dup hash stays -- it prevents re-sending
        the same sentence twice, which is a different problem from flooding.
        """
        import hashlib

        node = self.registry.get(target)
        if node is None:
            return {"ok": False, "error": f"未知子代理: {target}"}
        if node.is_terminal:
            return {"ok": False,
                    "error": f"子代理 {node.name} 已结束（{node.status}），"
                             "请用 read_agent_result 取结果；需要继续工作请重新 spawn_agent"}
        text = (message or "").strip()
        if not text:
            return {"ok": False, "error": "message 不能为空"}
        if len(node.pending_messages) >= max_per_agent:
            return {"ok": False,
                    "error": f"该子代理还有 {len(node.pending_messages)} 条未处理的追加指令"
                             f"（上限 {max_per_agent} 条），等它消化后再发"}
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in node.followup_hashes:
            return {"ok": True, "deduped": True, "note": "同内容已投递过，未重复注入"}
        node.followup_hashes.add(digest) if len(node.followup_hashes) < 200 else None
        node.pending_messages.append({
            "role": "user",
            "content": f"[来自父代理的追加指令] {text}",
        })
        node.wake.set()
        return {"ok": True, "delivered": True,
                "note": "将在子代理下一次模型调用前注入"}

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
            if condition in ("any_event", "event") and self._has_new_event(nodes, since_event_seq):
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
        # any_event / event 由 _has_new_event 判定，这里只管「全部了结」。
        # 了结 = 终态 或 idle（本轮做完在等后续）。持久 worker 永远不终态，
        # 若这里只认 is_terminal，lead 的 all_terminal 会白等到超时。
        return all(n.is_settled for n in nodes)

    def _has_new_event(self, nodes: list[AgentNode], since_event_seq: int = 0) -> bool:
        """是否已有游标之后的实质进展（工具调用 / 注入指令）。"""
        since = int(since_event_seq or 0)
        for n in nodes:
            for rec in n.event_log:
                if rec["seq"] > since and rec["event"].get("type") in _WAKE_EVENTS:
                    return True
        return False

    def _wait_result(self, nodes: list[AgentNode], condition: str, timed_out: bool,
                     since_event_seq: int = 0) -> dict:
        remaining = [n.name for n in nodes if not n.is_settled]
        idle = [n.name for n in nodes if n.is_idle]
        refs = {n.name: n.run_id for n in nodes if n.is_settled}
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
        if not remaining:
            # 全都了结了。如果有 idle 的，说明「这轮干完了」但还活着 —— 与全部
            # 终态区分开，否则 lead 会以为该关掉它了。
            state = "ready" if idle else "all_terminal"
        elif timed_out:
            state = "timeout"
        else:
            state = "event"
        if timed_out:
            action = "wait_agent" if remaining else "read_agent_result"
        elif remaining:
            action = "wait_agent"
        else:
            action = "read_agent_result" if refs else "wait_agent"
        note = ""
        if timed_out:
            note = "等待超时不是失败：子代理仍在运行，继续 wait_agent 即可"
        elif state == "ready":
            note = ("子代理已完成这一轮并在等待后续任务（status=idle），"
                    "可用 read_agent_result 取本轮结论；给它新任务用 send_message")
        return {
            "state": state,
            "timed_out": timed_out,
            "condition": condition,
            "changed_agents": [n.to_dict() for n in nodes],
            "remaining_targets": remaining,
            "idle_targets": idle,
            "result_refs": refs,
            "recommended_next_action": action,
            "event_seq": max_seq,
            "events": incremental,
            "note": note,
        }

    # ---------------- read result ----------------
    async def read_result(self, target: str, max_chars: int = MAX_READ_CHARS) -> dict:
        node = self.registry.get(target)
        if node is None:
            # 跨 generation：旧节点已不在内存，回退到 DB 最近一次 run
            row = await sub_store.get_latest_run_by_name(
                self.ctx.user_id, self.ctx.session_id, target
            )
            if row and row.get("result_text"):
                return {
                    "ok": True,
                    "name": target,
                    "error": row.get("error") or "",
                    "result": row["result_text"][:max_chars],
                    "status": row.get("status", ""),
                    "turns": row.get("turns", 0),
                    "source": "db",
                    "note": "该子代理属于上一轮，结果来自持久化记录",
                }
            return {"ok": False, "error": f"未知子代理: {target}"}
        if not node.is_settled:
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
        out = {
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
        if node.is_idle:
            # idle 的 agent 仍可能被唤醒继续干，所以这只是一轮结论，不是最终答复。
            out["partial"] = True
            out["note"] = ("该 agent 已完成本轮并在等待后续任务，这是它最近一轮的结论；"
                           "需要它继续工作请用 send_message")
        return out

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
        # 不要无条件盖成 closed：_stop_node 可能已经把这个 agent 判成 completed
        # （空闲且干完活才被关）。盖掉会让「它成功干完一轮」这个事实消失。
        if not node.is_terminal:
            node.status = "closed"
        node.notify_waiters()
        self.registry.remove(node.name)
        self._touch()
        return {"ok": True, "closed": closed, "status": node.status}

    async def _stop_node(self, node: AgentNode, *, reason: str = "close") -> None:
        was_idle = node.is_idle
        # 记 completed 而不是 stopped 的两种情形：这个 agent 其实已经干完活了，
        # 被回收的只是它的槽位。记 stopped 会让前端显示「已停止」——用户会以为
        # 自己打断了它，而事实是它的任务已经完成、结果也交回去了。
        #   1. drain：本轮结束时的整体回收（所有节点统一处理）；
        #   2. close：lead 明确关闭一个**空闲且没有待办**的队友（典型是收完结果
        #      释放并发槽位）。此时若它正跑到一半、或还有没处理的消息，那就是
        #      真的被打断，必须保留 stopped。
        settled = was_idle and not node.has_pending_work
        reclaim = ((reason == "drain" or settled) and node.rounds > 0
                   and not node.error)
        if reclaim:
            # 状态先置 completed，再等它真的退出（finally 里会持久化这个值）；
            # 循环看到 stop 时会保留已有的终态，不会覆盖回 stopped。
            node.status = "completed"
        # 关闭是有成本的（最多等 2s 优雅退出）。先广播「停止中」，否则面板在这段
        # 时间里仍显示「运行中」，用户会以为关不掉。
        # 回收槽位（reclaim）时不推：它并不是被打断，推 stopping 会让前端先闪一下
        # 「停止中」再变「已完成」，与事实不符。
        elif not node.is_terminal and node.status != "stopping":
            node.status = "stopping"
            self._publish({
                "type": "subagent_status",
                "agent_id": node.agent_id,
                "name": node.name,
                "status": "stopping",
            })
        node.stop.set()
        node.wake.set()
        await self._join_task(node, grace_s=2.0)

    @staticmethod
    async def _join_task(node: AgentNode, *, grace_s: float) -> None:
        """Bounded join so the child's ``finally`` (lease, sidechain, finish_run) runs."""
        task = node.task
        if task is None or task.done():
            return
        for _ in range(max(1, int(grace_s * 10))):
            if task.done():
                return
            await asyncio.sleep(0.1)
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass

    # ---------------- lifecycle ----------------
    def has_active(self) -> bool:
        """Whether any child still needs a join before the SSE can close.

        An idle teammate has finished its work for now; keeping the SSE open for
        it would park the stream until ``TEAM_DRAIN_TIMEOUT_S`` (300 s) for no
        reason.  Use :meth:`has_live_nodes` for stop/close decisions, which must
        still see idle agents.
        """
        return any(not n.is_terminal and not n.is_idle
                   for n in self.registry.all_nodes())

    def has_live_nodes(self) -> bool:
        """Whether any child is still alive (idle included) and can be stopped."""
        return bool(self.registry.active_nodes())

    # ---------------- persistent worker: idle loop ----------------
    async def _idle_wait(self, node: AgentNode, *, timeout_s: float,
                         poll_s: float = 2.0) -> str:
        """Wait for work while idle; returns the reason it stopped waiting.

        Event-driven with a bounded poll as the safety net: a missed trigger
        costs one poll interval (≤ ``poll_s``) instead of leaving the teammate
        parked forever.  CC's 500 ms poll loop is the same fallback, just coarser
        here because Hlaudz has no per-teammate process to poll from.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        # 进入等待时的待处理数量。兜底轮询只在「数量变多」时返回 wake：
        # 否则一条一直没被消费的消息会让 _idle_wait 每次轮询都立刻返回，
        # 把外层循环变成忙循环（实测 2 秒 34 次空转）。
        baseline = len(node.pending_messages)
        while True:
            if node.stop.is_set() or node.is_terminal:
                return "stopped"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            try:
                await asyncio.wait_for(node.wake.wait(),
                                       timeout=min(poll_s, remaining))
                node.wake.clear()
                # 被唤醒后重新判定：可能是关闭，也可能是新消息/新任务
                if node.stop.is_set() or node.is_terminal:
                    return "stopped"
                return "wake"
            except asyncio.TimeoutError:
                if node.stop.is_set() or node.is_terminal:
                    return "stopped"
                if time.monotonic() >= deadline:
                    return "timeout"
                # 兜底：只有在“出现了之前没见过的消息”时才判定 wake，
                # 避免同一条未消费消息造成空转。
                if len(node.pending_messages) > baseline:
                    return "wake"

    def push_idle_notification(self, node: AgentNode, *, idle_reason: str = "available",
                               completed_task_id: str = "") -> None:
        """Tell the lead this teammate finished a round and is standing by.

        Deliberately distinct from ``subagent_end``: an idle teammate has not
        ended, and telling the lead "completed" would make it re-spawn a worker
        that is still there.
        """
        item = {
            "id": uuid.uuid4().hex[:16],
            "agent": node.name,
            "status": "空闲",
            "result_chars": len(node.result or ""),
            "preview": (node.result or "")[:400],
            "is_idle": True,
            "idle_reason": idle_reason,
            "completed_task_id": completed_task_id,
        }
        self._notifications.append(item)
        self._touch()

    def wake_idle_agents(self) -> None:
        """Wake every idle child: new work may have appeared on the board."""
        for n in self.registry.all_nodes():
            if n.is_idle:
                n.wake.set()

    def stop_all_now(self) -> None:
        """Synchronously signal/cancel every child task.

        Used from the parent's cancellation-safe ``finally`` so child tasks do
        not keep running (and holding quota) after the SSE task is cancelled.
        The actual unwind/lease release happens in each child's own finally.
        """
        for n in self.registry.active_nodes():
            # 已经干完至少一轮的节点：这是本轮结束时的整体回收，不是用户停止。
            # 先判成 completed，子任务取消时就不会把自己覆盖成 stopped。
            if n.rounds > 0 and not n.error:
                n.status = "completed"
            n.stop.set()
            n.wake.set()
            if n.task is not None and not n.task.done():
                n.task.cancel()
        self._touch()

    async def stop_all(self) -> None:
        """用户点「停止」：把本代所有子代理标记停止并取消。

        Cancellation must be followed by a short bounded join.  Merely calling
        task.cancel() leaves child tasks unwinding in the background; they can
        still hold quota leases / write sidechains after the parent generation
        is reported stopped.  Bounded wait keeps the stop endpoint responsive
        even if a tool ignores cancellation.
        """
        self.stop_all_now()
        tasks = [n.task for n in self.registry.active_nodes()
                 if n.task is not None and not n.task.done()]
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=1.5)
            except BaseException:
                pass

    async def close_all(self) -> None:
        self._closed = True
        for n in list(self.registry.all_nodes()):
            if not n.is_terminal:
                await self._stop_node(n, reason="drain")
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
