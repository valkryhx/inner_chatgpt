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
                 event_sink: Callable[[dict[str, Any]], None] | None = None):
        if not isinstance(scope, TeamScope):
            raise TypeError("scope must be a TeamScope")
        if scope.role not in ("lead", "teammate"):
            raise ValueError("invalid team role")
        self.scope = scope
        self._quota_manager = quota_manager
        self._spawn_fn = spawn_fn
        self._event_sink = event_sink
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
        """Create/bind an owner and invoke the injected spawn protocol.

        This module intentionally does not import the chat/subagent runtime.  The
        caller supplies ``spawn_fn``; if it fails, a task bound by this call is
        reclaimed using CAS so a failed spawn cannot leave a phantom owner.
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
            bound = await self.bind_task_owner(
                team_task_id, f"sub:{task_name}", expected_version=expected_version
            )
            if not bound.get("ok"):
                return bound
            task = bound.get("task")
        try:
            spawned = await self._spawn_fn(
                task_name=task_name, message=message, team_task_id=team_task_id,
                owner_agent_id=f"sub:{task_name}", max_turns=max_turns,
                scope=self.scope,
            )
        except Exception:
            if task is not None:
                await self.reclaim(team_task_id, expected_version=int(task["version"]))
            raise
        if isinstance(spawned, dict) and spawned.get("ok") is False:
            if task is not None:
                await self.reclaim(team_task_id, expected_version=int(task["version"]))
            return {"ok": False, "task": task, "spawn": spawned}
        return {"ok": True, "task": task, "spawn": spawned}

    async def send_message(self, *, recipient: str, content: str,
                           kind: str = "note") -> dict[str, Any]:
        if not (recipient or "").strip():
            raise TeamHostError("recipient is required")
        if not (content or "").strip():
            raise TeamHostError("content is required")
        if self.scope.role == "teammate" and recipient not in ("lead", "root"):
            raise TeamHostError("teammates may only message the lead")
        message = await team_store.send_message(
            self.scope, recipient=recipient, kind=kind, content=content
        )
        await self._observe_events()
        return message

    async def list_messages(self, *, recipient: str = "",
                            unread_only: bool = False,
                            limit: int = 100) -> list[dict[str, Any]]:
        # Teammates may only read their own inbox, even if a caller supplies a
        # different recipient.  The server-side actor wins over this argument.
        if self.scope.role == "teammate":
            recipient = self.scope.agent_id
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
