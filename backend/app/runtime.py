"""Process-level runtime: shared HistoryStore + per-user lazy RuntimeHub + stop registry."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from .agent import UserRuntime
from .history_store import get_history_store, HistoryStore

_hub: Optional["RuntimeHub"] = None


def set_hub(hub: Optional["RuntimeHub"]) -> None:
    global _hub
    _hub = hub


def get_hub() -> Optional["RuntimeHub"]:
    return _hub


class RuntimeHub:
    def __init__(self) -> None:
        self.store: Optional[HistoryStore] = None
        # key = (user_id, model_key); model_key = session's LLM config id ("" = follow default)
        self._runtimes: dict[tuple[str, str], UserRuntime] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        # 活跃的子代理宿主：key = (user_id, session_id, generation_id, team_id)
        # 用户 id 必须参与键，防止只猜 session/generation 就能影响别人。
        self._subagent_hosts: dict[tuple[str, str, str, str], "object"] = {}
        # 本代 ACP manager：key = (user_id, session_id, generation_id)
        # 与 subagent hosts 同构，用户 id 参与键，/stop 不能作用于别人的会话。
        self._acp_managers: dict[tuple[str, str, str], "object"] = {}

    # ---------------- acp managers ----------------
    def register_acp_manager(self, user_id: str, session_id: str, generation_id: str,
                             manager: "object") -> None:
        self._acp_managers[(user_id, session_id, generation_id)] = manager

    def unregister_acp_manager(self, user_id: str, session_id: str, generation_id: str) -> None:
        self._acp_managers.pop((user_id, session_id, generation_id), None)

    def get_acp_manager(self, user_id: str, session_id: str, generation_id: str):
        return self._acp_managers.get((user_id, session_id, generation_id))

    async def stop_acp_generation(self, user_id: str, session_id: str, generation_id: str) -> int:
        """优雅停止该代的 ACP 会话；未知代或 manager 抛错都返回 0。"""
        mgr = self._acp_managers.get((user_id, session_id, generation_id))
        if mgr is None:
            return 0
        try:
            return await mgr.stop_generation()
        except Exception:
            return 0

    # ---------------- subagent hosts ----------------
    def register_subagent_host(self, user_id: str, session_id: str, generation_id: str,
                               team_id: str, host: "object") -> None:
        self._subagent_hosts[(user_id, session_id, generation_id, team_id)] = host

    def unregister_subagent_host(self, user_id: str, session_id: str, generation_id: str,
                                 team_id: str = "") -> None:
        self._subagent_hosts.pop((user_id, session_id, generation_id, team_id), None)

    def get_subagent_host(self, user_id: str, session_id: str, generation_id: str, team_id: str = ""):
        return self._subagent_hosts.get((user_id, session_id, generation_id, team_id))

    async def stop_subagent_generation(self, user_id: str, session_id: str, generation_id: str) -> int:
        """级联停止：只匹配该用户、该会话、该代的所有 host。"""
        keys = [k for k in self._subagent_hosts
                if k[0] == user_id and k[1] == session_id and k[2] == generation_id]
        hosts = [self._subagent_hosts[k] for k in keys]
        if not hosts:
            return 0
        n = 0
        for host in hosts:
            try:
                n += len(host.list_agents())
                await host.stop_all()
            except Exception:
                pass
        return n

        if host is None:
            return 0
        try:
            n = len(host.list_agents())
            await host.stop_all()
            return n
        except Exception:
            return 0

    async def startup(self) -> None:
        self.store = get_history_store()
        await self.store.init()

    async def shutdown(self) -> None:
        for host in list(self._subagent_hosts.values()):
            try:
                await host.close_all()
            except Exception:
                pass
        self._subagent_hosts.clear()
        for mgr in list(self._acp_managers.values()):
            try:
                await mgr.close_all()
            except Exception:
                pass
        self._acp_managers.clear()
        for key in list(self._runtimes.keys()):
            await self._runtimes[key].close()
        self._runtimes.clear()
        self.store = None

    async def get_user_runtime(self, user_id: str, model_id: str = "") -> UserRuntime:
        """Get/build runtime for (user, session-selected model)."""
        key = (user_id, model_id or "")
        cur = self._runtimes.get(key)
        if cur is not None:
            return cur
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cur = self._runtimes.get(key)
            if cur is not None:
                return cur
            ur = UserRuntime()
            await ur.build(user_id, model_id or None)
            self._runtimes[key] = ur
            return ur

    async def invalidate(self, user_id: str) -> None:
        for key in list(self._runtimes.keys()):
            if key[0] == user_id:
                ur = self._runtimes.pop(key, None)
                if ur is not None:
                    await ur.close()

    async def invalidate_all(self) -> int:
        """Drop every cached runtime so the next turn rebuilds system prompts.

        Needed after a change that affects ALL users — chiefly importing a
        shared/public skill, whose L1 index is baked into each user's system
        prompt at build time. Returns how many runtimes were dropped.
        """
        keys = list(self._runtimes.keys())
        dropped = 0
        for key in keys:
            ur = self._runtimes.pop(key, None)
            if ur is not None:
                dropped += 1
                try:
                    await ur.close()
                except Exception:
                    pass
        return dropped


# ---------------- stop registry ----------------
# Production keys include the authenticated owner.  Session IDs are globally
# unique, but owner scoping is still enforced so a caller can never act on a
# guessed foreign key.
_stop_events: dict[tuple[str, str, str], asyncio.Event] = {}
# The streaming response is an asyncio task.  A cooperative flag alone cannot
# unblock a task that is awaiting an LLM read or a tool dispatch, so keep the
# task reference too and cancel it explicitly from the stop endpoint.
_generation_tasks: dict[tuple[str, str, str], asyncio.Task] = {}
# A request that has acquired the DB lease but whose StreamingResponse has not
# started iterating yet is still live.  Do not mistake that tiny window for a
# stale lease when a second tab sends concurrently.
_generation_reservations: dict[tuple[str, str, str], float] = {}
# Stop can arrive during the pre-stream prelude (runtime build / compaction),
# before register_generation has captured the actual asyncio task.  Remember
# the explicit intent so register_generation returns an already-set event.
_stop_requested: dict[tuple[str, str, str], float] = {}
_STOP_INTENT_TTL_S = 300.0
# A reservation only bridges the tiny gap between accepting an HTTP request
# and the StreamingResponse task entering the generator.  If a client never
# consumes the response body, the reservation must not block the session
# forever.  Thirty seconds is far longer than that hand-off window.
_RESERVATION_TTL_S = 30.0


def _key(user_id: str, session_id: str, generation_id: str) -> tuple[str, str, str]:
    return (user_id, session_id, generation_id)


def _cleanup_expired_reservations() -> None:
    cutoff = time.monotonic() - _RESERVATION_TTL_S
    for key, created in list(_generation_reservations.items()):
        if created < cutoff:
            _generation_reservations.pop(key, None)
    intent_cutoff = time.monotonic() - _STOP_INTENT_TTL_S
    for key, created in list(_stop_requested.items()):
        if created < intent_cutoff:
            _stop_requested.pop(key, None)


def _is_live_key(key: tuple[str, str, str]) -> bool:
    """Return true only for a task that can still run, or a fresh hand-off."""
    task = _generation_tasks.get(key)
    if task is not None and not task.done():
        return True
    if key in _stop_events:
        # The generator owns the event and removes it in its finally block.
        # If it is gone while the task is already done, the lease is stale.
        return False
    created = _generation_reservations.get(key)
    if created is None:
        return False
    if time.monotonic() - created >= _RESERVATION_TTL_S:
        _generation_reservations.pop(key, None)
        return False
    return True


def has_active_generation(session_id: str, user_id: str = "") -> bool:
    _cleanup_expired_reservations()
    for key in set(_stop_events) | set(_generation_tasks) | set(_generation_reservations):
        owner, sid, _gid = key
        if sid != session_id:
            continue
        if user_id and owner != user_id:
            continue
        if _is_live_key(key):
            return True
    return False


def is_generation_live(user_id: str, session_id: str, generation_id: str) -> bool:
    _cleanup_expired_reservations()
    return _is_live_key(_key(user_id, session_id, generation_id))


def reserve_generation(user_id: str, session_id: str, generation_id: str) -> None:
    """Mark an accepted request live before its stream task starts iterating."""
    _generation_reservations[_key(user_id, session_id, generation_id)] = time.monotonic()


def release_generation_reservation(user_id: str, session_id: str, generation_id: str) -> None:
    _generation_reservations.pop(_key(user_id, session_id, generation_id), None)


def register_generation(user_id: str, session_id: str, generation_id: str) -> asyncio.Event:
    key = _key(user_id, session_id, generation_id)
    ev = asyncio.Event()
    # Consume a tombstone only when the stream actually registers.  A stop can
    # arrive and call unregister_generation() before this function runs (client
    # aborts immediately after receiving headers); that must not erase intent.
    if _stop_requested.pop(key, None) is not None:
        ev.set()
    _stop_events[key] = ev
    _generation_reservations[key] = time.monotonic()
    task = asyncio.current_task()
    if task is not None:
        _generation_tasks[key] = task
    return ev


def unregister_generation(user_id: str, session_id: str, generation_id: str) -> None:
    key = _key(user_id, session_id, generation_id)
    _stop_events.pop(key, None)
    _generation_tasks.pop(key, None)
    _generation_reservations.pop(key, None)
    # Do not discard a *pending* tombstone here; the pre-stream stop path calls
    # unregister_generation() before StreamingResponse first iterates, and the
    # later register_generation() must still observe the stop intent.  However,
    # once this generation actually registered (event exists / task captured),
    # unregister marks normal completion and the tombstone must be cleared so a
    # reused generation id in tests or a future retry does not inherit a stop.
    _stop_requested.pop(key, None)


def request_stop(user_id: str, session_id: str, generation_id: str | None = None) -> int:
    n = 0
    for key, ev in list(_stop_events.items()):
        owner, sid, gid = key
        if owner != user_id or sid != session_id:
            continue
        if generation_id and gid != generation_id:
            continue
        ev.set()
        n += 1
    if generation_id:
        # Record intent even when the stream has not started registering yet.
        _stop_requested[_key(user_id, session_id, generation_id)] = time.monotonic()
    return n


def cancel_generation(user_id: str, session_id: str, generation_id: str) -> int:
    """Cancel the actual streaming task in addition to setting the stop flag.

    Cancellation is what makes stop reliable while the request is blocked in
    an LLM socket or a tool wait.  The event flag stays for code paths that
    still use cooperative checks.
    """
    key = _key(user_id, session_id, generation_id)
    ev = _stop_events.get(key)
    if ev is not None:
        ev.set()
    task = _generation_tasks.get(key)
    if task is None or task.done():
        # Pre-stream prelude: make sure register_generation sees the intent.
        _stop_requested[key] = time.monotonic()
        return 0
    task.cancel()
    return 1
