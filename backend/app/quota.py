"""Four-level process-local capacity gates.

Order is fixed: global -> user -> team -> agent.  The manager is intentionally
in-process for the single-instance MVP; durable team state remains in SQLite.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from .scope import TeamScope


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Limits:
    global_teams: int = _env_int("GLOBAL_MAX_ACTIVE_TEAMS", 16)
    # 每用户上限不是「一人只能开一个 Team」。会话级 SESSION_MAX_ACTIVE_TEAMS=1
    # 已经保证一个会话只有一个活跃 Team，用户级再卡 1 的后果是：同一用户的
    # 两个标签页 / 两个会话互相排斥，后来的那一轮直接 team_unavailable。
    # 用户级闸门的本意是「多用户之间的公平性」，所以默认放到 4。
    user_teams: int = _env_int("USER_MAX_ACTIVE_TEAMS", 4)
    session_teams: int = _env_int("SESSION_MAX_ACTIVE_TEAMS", 1)
    team_subagents: int = _env_int("TEAM_MAX_ACTIVE_SUBAGENTS", 4)
    user_subagents: int = _env_int("USER_MAX_ACTIVE_SUBAGENTS", 8)
    global_subagents: int = _env_int("GLOBAL_MAX_ACTIVE_SUBAGENTS", 64)
    user_code_runs: int = _env_int("USER_MAX_CODE_RUNS", 2)
    global_code_runs: int = _env_int("GLOBAL_MAX_CODE_RUNS", 8)
    user_mcp_calls: int = _env_int("USER_MAX_MCP_CALLS", 2)
    global_mcp_calls: int = _env_int("GLOBAL_MAX_MCP_CALLS", 32)
    user_acp_active: int = _env_int("INNER_ACP_MAX_ACTIVE_USER", 4)
    global_acp_active: int = _env_int("INNER_ACP_MAX_ACTIVE_GLOBAL", 80)


class QuotaExceeded(RuntimeError):
    pass


class Lease:
    def __init__(self, manager: "QuotaManager", semaphores: list[asyncio.Semaphore]) -> None:
        self._manager = manager
        self._semaphores = semaphores
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        for sem in reversed(self._semaphores):
            sem.release()


class QuotaManager:
    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = limits or Limits()
        self._global_teams = asyncio.Semaphore(self.limits.global_teams)
        self._user_teams: dict[str, asyncio.Semaphore] = {}
        self._session_teams: dict[tuple[str, str], asyncio.Semaphore] = {}
        self._team_subagents: dict[tuple[str, str, str, str], asyncio.Semaphore] = {}
        self._user_subagents: dict[str, asyncio.Semaphore] = {}
        self._global_subagents = asyncio.Semaphore(self.limits.global_subagents)
        self._user_code_runs: dict[str, asyncio.Semaphore] = {}
        self._global_code_runs = asyncio.Semaphore(self.limits.global_code_runs)
        self._user_mcp_calls: dict[str, asyncio.Semaphore] = {}
        self._global_mcp_calls = asyncio.Semaphore(self.limits.global_mcp_calls)
        self._user_acp_active: dict[str, asyncio.Semaphore] = {}
        self._global_acp_active = asyncio.Semaphore(self.limits.global_acp_active)

    def _user(self, table: dict[str, asyncio.Semaphore], key: str, value: int) -> asyncio.Semaphore:
        sem = table.get(key)
        if sem is None:
            sem = asyncio.Semaphore(value)
            table[key] = sem
        return sem

    async def _acquire_many(self, semaphores: list[asyncio.Semaphore], timeout_s: float) -> Lease:
        got: list[asyncio.Semaphore] = []
        try:
            async def one(sem: asyncio.Semaphore):
                await asyncio.wait_for(sem.acquire(), timeout=max(0.01, timeout_s))
                got.append(sem)
            if timeout_s <= 0:
                for sem in semaphores:
                    await sem.acquire()
                    got.append(sem)
            else:
                await asyncio.wait_for(asyncio.gather(*(one(sem) for sem in semaphores)), timeout=timeout_s)
            return Lease(self, got)
        except (asyncio.TimeoutError, TimeoutError):
            for sem in reversed(got):
                sem.release()
            raise QuotaExceeded(f"capacity_exceeded after {timeout_s:.1f}s")

    async def team_lease(self, scope: TeamScope, *, timeout_s: float = 0.0) -> Lease:
        """Acquire a long-lived active-team slot; owner must release explicitly."""
        gates = (
            ("全局 Team", self._global_teams, self.limits.global_teams),
            ("用户 Team", self._user(self._user_teams, scope.user_id, self.limits.user_teams),
             self.limits.user_teams),
            ("会话 Team", self._user(self._session_teams, (scope.user_id, scope.session_id),
                                     self.limits.session_teams),
             self.limits.session_teams),
        )
        sems = [sem for _, sem, _ in gates]
        try:
            return await self._acquire_many(sems, timeout_s)
        except QuotaExceeded as exc:
            # 报错要能区分是「同会话已有 Team / 同用户别的会话占着 / 全局满了」。
            # 只给一句 capacity_exceeded 时，用户分不清是自己多开标签页撞车，
            # 还是租约真的泄漏了 —— 这两种的处置方式完全不同。
            # 水位取在失败**之后**，才是导致这次失败的真实状态。
            snapshot = "，".join(
                f"{label} {sem._value}/{limit}" for label, sem, limit in gates
            )
            raise QuotaExceeded(
                f"{exc} [{snapshot}；user={scope.user_id} session={scope.session_id}]"
            ) from None

    @asynccontextmanager
    async def team(self, scope: TeamScope, *, timeout_s: float = 0.0) -> AsyncIterator[Lease]:
        lease = await self.team_lease(scope, timeout_s=timeout_s)
        try:
            yield lease
        finally:
            lease.release()

    @asynccontextmanager
    async def subagent(self, scope: TeamScope, *, timeout_s: float = 0.0) -> AsyncIterator[Lease]:
        sems = [
            self._global_subagents,
            self._user(self._user_subagents, scope.user_id, self.limits.user_subagents),
            self._user(self._team_subagents, scope.key(), self.limits.team_subagents),
        ]
        lease = await self._acquire_many(sems, timeout_s)
        try:
            yield lease
        finally:
            lease.release()

    @asynccontextmanager
    async def code_run(self, user_id: str, *, timeout_s: float = 0.0) -> AsyncIterator[Lease]:
        sems = [
            self._global_code_runs,
            self._user(self._user_code_runs, user_id, self.limits.user_code_runs),
        ]
        lease = await self._acquire_many(sems, timeout_s)
        try:
            yield lease
        finally:
            lease.release()

    async def subagent_lease(self, scope: TeamScope, *, timeout_s: float = 0.0) -> Lease:
        """Acquire a long-lived subagent slot (released by its owner later)."""
        sems = [
            self._global_subagents,
            self._user(self._user_subagents, scope.user_id, self.limits.user_subagents),
            self._user(self._team_subagents, scope.key(), self.limits.team_subagents),
        ]
        return await self._acquire_many(sems, timeout_s)

    @asynccontextmanager
    async def mcp_call(self, user_id: str, *, timeout_s: float = 0.0) -> AsyncIterator[Lease]:
        sems = [
            self._global_mcp_calls,
            self._user(self._user_mcp_calls, user_id, self.limits.user_mcp_calls),
        ]
        lease = await self._acquire_many(sems, timeout_s)
        try:
            yield lease
        finally:
            lease.release()

    @asynccontextmanager
    async def acp_active(self, user_id: str, *, timeout_s: float = 2.0) -> AsyncIterator[Lease]:
        sems = [
            self._global_acp_active,
            self._user(self._user_acp_active, user_id, self.limits.user_acp_active),
        ]
        lease = await self._acquire_many(sems, timeout_s)
        try:
            yield lease
        finally:
            lease.release()


_quota: QuotaManager | None = None


def get_quota() -> QuotaManager:
    global _quota
    if _quota is None:
        _quota = QuotaManager()
    return _quota
