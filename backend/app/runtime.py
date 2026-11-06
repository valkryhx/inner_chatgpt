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
        # 活跃的子代理宿主：key = (user_id, session_id, generation_id, team_id)
        # 用户 id 必须参与键，防止只猜 session/generation 就能影响别人。
        self._subagent_hosts: dict[tuple[str, str, str, str], "object"] = {}

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


def has_active_generation(session_id: str) -> bool:
    return any(sid == session_id for _, sid, _ in _stop_events)


def register_generation(user_id: str, session_id: str, generation_id: str) -> asyncio.Event:
    ev = asyncio.Event()
    _stop_events[(user_id, session_id, generation_id)] = ev
    return ev


def unregister_generation(user_id: str, session_id: str, generation_id: str) -> None:
    _stop_events.pop((user_id, session_id, generation_id), None)


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
    return n
