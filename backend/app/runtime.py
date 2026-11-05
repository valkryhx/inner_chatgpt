"""Process-level runtime: shared HistoryStore + per-user lazy RuntimeHub + stop registry."""
from __future__ import annotations

import asyncio
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

    async def startup(self) -> None:
        self.store = get_history_store()
        await self.store.init()

    async def shutdown(self) -> None:
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
_stop_events: dict[str, dict[str, asyncio.Event]] = {}


def has_active_generation(session_id: str) -> bool:
    return bool(_stop_events.get(session_id))


def register_generation(session_id: str, generation_id: str) -> asyncio.Event:
    ev = asyncio.Event()
    _stop_events.setdefault(session_id, {})[generation_id] = ev
    return ev


def unregister_generation(session_id: str, generation_id: str) -> None:
    d = _stop_events.get(session_id)
    if d:
        d.pop(generation_id, None)
        if not d:
            _stop_events.pop(session_id, None)


def request_stop(session_id: str, generation_id: Optional[str] = None) -> int:
    d = _stop_events.get(session_id)
    if not d:
        return 0
    targets = [generation_id] if generation_id else list(d.keys())
    n = 0
    for gid in targets:
        ev = d.get(gid)
        if ev is not None:
            ev.set()
            n += 1
    return n
