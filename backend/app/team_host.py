"""Durable Agent Team host.

The host is the only service layer for Team facts.  A host instance is bound to
an immutable :class:`TeamScope`; every store call therefore carries the complete
user/session/generation/team identity.  No method accepts an alternative scope
from a tool argument or an HTTP payload.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from . import team_store
from .scope import TeamScope


class TeamHostError(RuntimeError):
    """A team operation was rejected by ownership, state or CAS rules."""


class TeamHost:
    """Service facade for one durable team.

    ``quota_manager`` is resolved lazily so tests and future callers can inject
    a fake manager.  A lead acquires one long-lived generation lease in
    :meth:`acquire_lifecycle`; the lease is released only by ``close()`` (or by
    the generation's failure path).  This is what enforces the per-user and
    per-session active-team limits across concurrent sessions.
    """

    def __init__(self, scope: TeamScope, *, quota_manager=None,
                 spawn_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
                 event_sink: Callable[[dict[str, Any]], None] | None = None,
                 deliver_sink: Callable[[dict[str, Any]], dict[str, Any]] | None = None):
        if not isinstance(scope, TeamScope):
            raise TypeError("scope must be a TeamScope")
        if scope.role not in ("lead", "teammate"):
            raise ValueError("invalid team role")
        self.scope = scope
        self._quota_manager = quota_manager
        self._spawn_fn = spawn_fn
        self._event_sink = event_sink
        # Delivers a stored message into a live agent's pending queue.  Injected
        # by the turn runtime, which is the only layer that knows which agents
        # are currently alive in this generation.
        self._deliver_sink = deliver_sink
        self._lifecycle_lease = None
        # Every durable event read/write through this host is mirrored to the
        # SSE sink.  A cursor prevents replay of events already seen by a
        # status() call or a previous tool result.
        self._observed_seq = 0

    @property
    def quota_manager(self):
        if self._quota_manager is None:
            from .quota import get_quota
            self._quota_manager = get_quota()
        return self._quota_manager

    @property
    def team_id(self) -> str:
        return self.scope.team_id

    @property
    def role(self) -> str:
        return self.scope.role

    @property
    def observed_seq(self) -> int:
        """Read-only cursor exposed for generation-final event draining."""
        return self._observed_seq

    def _require_lead(self) -> None:
        if self.scope.role != "lead":
            raise TeamHostError("only the team lead may perform this action")

    async def load(self) -> dict[str, Any] | None:
        return await team_store.get_team(self.scope)

    def _publish_event(self, event: dict[str, Any]) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(self.event_shape(event))
        except Exception:
            # SSE rendering must never break a durable task mutation.
            pass

    async def _observe_events(self) -> list[dict[str, Any]]:
        rows = await team_store.events_since(
            self.scope, self._observed_seq, limit=200
        )
        for row in rows:
            self._observed_seq = max(self._observed_seq, int(row.get("event_seq", 0)))
            self._publish_event(row)
        return rows

    async def _emit(self, event_type: str, payload: dict[str, Any], *,
                    task_id: str = "") -> dict[str, Any]:
        row = await team_store.emit_event(
            self.scope, event_type, payload, task_id=task_id
        )
        self._observed_seq = max(self._observed_seq, int(row.get("event_seq", 0)))
        self._publish_event(row)
        return row

    async def acquire_lifecycle(self, *, timeout_s: float = 2.0) -> None:
        """Reserve this user/session's active-team slot for the generation.

        The slot must outlive the create critical section.  Otherwise two tabs
        could both pass a short gate and keep two active teams alive.  Calling
        this method twice on one host is idempotent.
        """
        self._require_lead()
        if self._lifecycle_lease is not None:
            return
        self._lifecycle_lease = await self.quota_manager.team_lease(
            self.scope, timeout_s=timeout_s
        )

    def release_lifecycle_now(self) -> None:
        """Synchronously release the active-team slot.

        This must be safe to call from a cancelled task's ``finally``: it does
        not await, so anyio/asyncio cancellation cannot interrupt it.  Skipping
        this release leaks the per-user Team quota until the process restarts.
        Idempotent by design.
        """
        self._require_lead()
        lease, self._lifecycle_lease = self._lifecycle_lease, None
        if lease is not None:
            lease.release()

    async def release_lifecycle(self) -> None:
        # Kept for callers that already await it; delegation keeps the release
        # synchronous and idempotent.
        self.release_lifecycle_now()

    async def create(self, name: str = "Agent Team") -> dict[str, Any]:
        """Create the team and return its status snapshot."""
        self._require_lead()
        existing = await self.load()
        if existing is not None:
            if self._lifecycle_lease is None:
                await self.acquire_lifecycle()
            await self._observe_events()
            return await self.status()
        await self.acquire_lifecycle()
        existing = await self.load()
        if existing is None:
            await team_store.create_team(self.scope, name=name)
        await self._observe_events()
        return await self.status()

    async def create_or_load(self, name: str = "Agent Team") -> dict[str, Any]:
        return await self.create(name)

    async def status(self) -> dict[str, Any]:
        team = await self.load()
        if team is None:
            if self.scope.role == "teammate":
                raise TeamHostError("team not found")
            return {"team": None, "tasks": [], "events": [], "messages": []}
        tasks = await team_store.list_tasks(self.scope)
        events = await self._observe_events()
        messages = await team_store.list_messages(self.scope, limit=100)
        return {"team": team, "tasks": tasks, "events": events, "messages": messages}

    async def create_task(self, *, subject: str, description: str = "",
                          blocked_by: list[str] | None = None) -> dict[str, Any]:
        self._require_lead()
        if not (subject or "").strip():
            raise TeamHostError("subject is required")
        if await self.load() is None:
            raise TeamHostError("team not found")
        try:
            task = await team_store.create_task(
                self.scope, subject=subject, description=description,
                blocked_by=blocked_by,
            )
            await self._observe_events()
            return task
        except ValueError as exc:
            raise TeamHostError(str(exc)) from exc

    async def list_tasks(self, *, status: str = "") -> list[dict[str, Any]]:
        return await team_store.list_tasks(self.scope, status=status)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await team_store.get_task(self.scope, task_id)

    async def claim(self, task_id: str, *, expected_version: int) -> dict[str, Any]:
        if self.scope.role == "teammate":
            task = await self.get_task(task_id)
            if task is None:
                return {"ok": False, "reason": "not_found"}
        result = await team_store.claim_task(
            self.scope, task_id, expected_version=expected_version
        )
        if result.get("ok"):
            await self._observe_events()
        return result

    async def _teammate_task_or_not_found(self, task_id: str, *,
                                           require_owner: bool = False) -> dict[str, Any] | None:
        if self.scope.role != "teammate":
            return None
        task = await self.get_task(task_id)
        if task is None:
            return {"ok": False, "reason": "not_found"}
        if require_owner and task.get("owner_agent_id") != self.scope.agent_id:
            # Do not reveal whether someone else owns the task.
            return {"ok": False, "reason": "not_found"}
        return None

    async def complete(self, task_id: str, *, expected_version: int) -> dict[str, Any]:
        missing = await self._teammate_task_or_not_found(task_id, require_owner=True)
        if missing is not None:
            return missing
        result = await team_store.complete_task(
            self.scope, task_id, expected_version=expected_version
        )
        if result.get("ok"):
            await self._observe_events()
        return result

    async def reclaim(self, task_id: str, *, expected_version: int) -> dict[str, Any]:
        missing = await self._teammate_task_or_not_found(task_id, require_owner=True)
        if missing is not None:
            return missing
        result = await team_store.reclaim_task(
            self.scope, task_id, expected_version=expected_version
        )
        if result.get("ok"):
            await self._observe_events()
        return result

    async def update_task(self, task_id: str, action: str, *,
                          expected_version: int) -> dict[str, Any]:
        action = (action or "").strip().lower()
        if action == "claim":
            return await self.claim(task_id, expected_version=expected_version)
        if action == "complete":
            return await self.complete(task_id, expected_version=expected_version)
        if action == "reclaim":
            return await self.reclaim(task_id, expected_version=expected_version)
        raise TeamHostError("action must be claim, complete or reclaim")

    async def bind_task_owner(self, task_id: str, owner_agent_id: str, *,
                              expected_version: int) -> dict[str, Any]:
        self._require_lead()
        if not (owner_agent_id or "").strip():
            raise TeamHostError("owner_agent_id is required")
        result = await team_store.bind_task_owner(
            self.scope, task_id, owner_agent_id, expected_version=expected_version
        )
        if result.get("ok"):
            await self._observe_events()
        return result

    async def spawn_teammate(self, *, task_name: str, message: str,
                             team_task_id: str = "",
                             expected_version: int | None = None,
                             max_turns: int = 0) -> dict[str, Any]:
        """Spawn a teammate and bind it to a team task.

        Order matters: the agent is spawned **first**, and the task is bound to
        the agent id the registry actually assigned.  Binding the requested name
        up front looks equivalent but is not -- a name that was used before gets
        a suffix (``worker`` -> ``worker_2``), so the task ended up owned by an
        id no live agent has.  The teammate then could not complete its own task
        and the lead had to do it.

        If the bind fails (blocked or a version conflict) the just-spawned agent
        is closed again, so a rejected spawn leaves nothing behind.
        """
        self._require_lead()
        if self._spawn_fn is None:
            raise TeamHostError("spawn_fn is not configured")
        if not (task_name or "").strip():
            raise TeamHostError("task_name is required")

        task = None
        if team_task_id:
            task = await self.get_task(team_task_id)
            if task is None:
                raise TeamHostError("task not found")
            if expected_version is None:
                expected_version = int(task.get("version", 0))
            # Refuse before spawning: a blocked task must not cost a whole agent
            # start-stop cycle just to be rejected at the bind step.
            if await team_store.task_is_blocked(self.scope, team_task_id):
                return {"ok": False, "task": task, "reason": "blocked",
                        "note": "任务依赖（blocked_by）尚未完成，未派活；"
                                "先完成被依赖的任务再绑定"}

        try:
            spawned = await self._spawn_fn(
                task_name=task_name, message=message, team_task_id=team_task_id,
                # A hint only: the registry may suffix a reused name, so the
                # authoritative owner is whatever the spawn returns.
                owner_agent_id=f"sub:{task_name}", max_turns=max_turns,
                scope=self.scope,
            )
        except Exception:
            raise
        if isinstance(spawned, dict) and spawned.get("ok") is False:
            return {"ok": False, "task": task, "spawn": spawned}

        if task is not None:
            # Bind to the real identity, not the requested one.
            owner = ""
            if isinstance(spawned, dict):
                owner = str(spawned.get("agent_id") or "")
                if not owner:
                    actual = str(spawned.get("name") or "")
                    owner = f"sub:{actual}" if actual else ""
            owner = owner or f"sub:{task_name}"
            try:
                bound = await self.bind_task_owner(
                    team_task_id, owner, expected_version=int(expected_version or 0))
            except Exception:
                await self._close_spawned(spawned)
                raise
            if not bound.get("ok"):
                await self._close_spawned(spawned)
                return {"ok": False, "task": task,
                        "reason": bound.get("reason", ""),
                        "spawn": spawned,
                        "note": ("任务依赖未完成或版本冲突，已撤销本次派活；"
                                 "先完成 blocked_by 指向的任务再派")}
            task = bound.get("task")
            if isinstance(spawned, dict):
                spawned["agent_id"] = owner
                spawned["task_id"] = team_task_id
            await self._pin_owner(owner, task_name)
        return {"ok": True, "task": task, "spawn": spawned}

    async def _pin_owner(self, agent_id: str, task_name: str) -> None:
        """Pin the spawn name to this agent so later sends cannot cross-talk."""
        name = team_store.agent_name_of(agent_id)
        for key in {name, task_name}:
            try:
                await team_store.pin_name(self.scope, key, agent_id)
            except ValueError:
                # Already pinned to a different agent: keep the existing pin
                # rather than rebinding, which would be the cross-talk path.
                pass

    async def _close_spawned(self, spawned: Any) -> None:
        """Best-effort teardown of an agent whose task binding was refused."""
        if not isinstance(spawned, dict):
            return
        closer = getattr(self, "_close_fn", None)
        if not callable(closer):
            return
        try:
            await closer(str(spawned.get("name") or ""))
        except Exception:  # noqa: BLE001 - cleanup must not mask the real error
            pass

    async def send_message(self, *, recipient: str, content: str,
                           kind: str = "note") -> dict[str, Any]:
        if not (recipient or "").strip():
            raise TeamHostError("recipient is required")
        if not (content or "").strip():
            raise TeamHostError("content is required")
        if self.scope.role == "teammate" and recipient not in ("lead", "root"):
            if team_store.normalize_agent_id(recipient) != team_store.LEAD_AGENT_ID:
                raise TeamHostError("teammates may only message the lead")

        # Resolve through the pin table so a bare name, ``sub:<name>`` and a
        # previously-pinned name all reach the same inbox. Delivery itself is
        # driven by whichever host owns the live agent (see deliver_pending).
        resolved = await team_store.resolve_name(self.scope, recipient)
        message = await team_store.send_message(
            self.scope, recipient=resolved or recipient, kind=kind, content=content
        )
        delivered = self._deliver(message)
        message["live"] = bool(delivered.get("delivered"))
        if delivered.get("delivered"):
            await team_store.mark_messages_delivered(
                self.scope, [message["message_id"]])
            message["state"] = "delivered"
        if not message["live"]:
            message["delivery_note"] = (
                "当前没有活着的 agent 占用这个名字；消息已入库，"
                "对方下一轮 turn 开始时会被投递"
            )
        await self._observe_events()
        return message

    def _deliver(self, message: dict[str, Any]) -> dict[str, Any]:
        """Push a just-stored message into a live agent's pending queue.

        Writing the row is not delivery: without this the model only ever sees
        teammate messages if it happens to call ``list_messages``, which is what
        made "cooperation" depend on the model remembering to poll. The sink is
        injected by the turn runtime, which owns both the lead loop and the
        child agents.
        """
        if self._deliver_sink is None:
            return {"delivered": False, "reason": "no delivery sink (no live agent)"}
        try:
            return self._deliver_sink(message) or {"delivered": False, "reason": "empty result"}
        except Exception as exc:  # noqa: BLE001 - delivery must not lose the stored row
            return {"delivered": False, "reason": f"{type(exc).__name__}: {exc}"}

    async def consume_inbox(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Take and mark-consumed this agent's pending messages."""
        return await team_store.consume_inbox(self.scope, limit=limit)

    async def pin_name(self, name: str, agent_id: str) -> dict[str, Any]:
        try:
            return await team_store.pin_name(self.scope, name, agent_id)
        except ValueError as exc:
            raise TeamHostError(str(exc)) from exc

    async def resolve_name(self, name: str) -> str:
        return await team_store.resolve_name(self.scope, name)

    async def list_messages(self, *, recipient: str = "",
                            unread_only: bool = False,
                            limit: int = 100) -> list[dict[str, Any]]:
        # Teammates may only read their own inbox, even if a caller supplies a
        # different recipient.  The server-side actor wins over this argument.
        if self.scope.role == "teammate":
            recipient = self.scope.agent_id
        elif recipient:
            # Read the same namespace the writer used: a message stored for
            # "root" must be found by recipient="lead" (and vice versa).
            recipient = team_store.normalize_agent_id(recipient) or recipient
        return await team_store.list_messages(
            self.scope, recipient=recipient,
            unread_only=unread_only, limit=limit,
        )

    async def set_status(self, status: str) -> bool:
        self._require_lead()
        if status not in team_store.TEAM_STATUSES:
            raise TeamHostError(f"invalid team status: {status}")
        ok = await team_store.set_team_status(self.scope, status)
        if ok:
            team = await self.load() or {}
            await self._emit(
                "team_end",
                {"status": status, "team": team},
            )
        return ok

    async def events_since(self, since_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        rows = await team_store.events_since(self.scope, since_seq, limit)
        for row in rows:
            self._observed_seq = max(self._observed_seq, int(row.get("event_seq", 0)))
            # 必须 publish：否则 teammate 侧产生的事件只推进游标、永远进不了 SSE
            self._publish_event(row)
        return rows

    @staticmethod
    def event_shape(event: dict[str, Any]) -> dict[str, Any]:
        """Normalize stored event rows to the shared Team SSE-compatible shape."""
        payload = event.get("payload")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(payload or "{}")
            except Exception:
                payload = {}
        return {
            "team_id": event.get("team_id", ""),
            "session_id": event.get("session_id", ""),
            "generation_id": event.get("generation_id", ""),
            "agent_id": event.get("agent_id", ""),
            "task_id": event.get("task_id", ""),
            "event_seq": event.get("event_seq", 0),
            "type": event.get("event_type") or event.get("type") or "team_event",
            "payload": payload,
            "status": payload.get("status", ""),
            "updated_at": event.get("created_at", 0),
        }
