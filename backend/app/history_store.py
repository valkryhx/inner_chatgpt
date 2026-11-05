"""Session history store (single authoritative history; replaces langgraph checkpointer).

One row per session: session_history(session_id, history JSON, updated_at).
History is a list[dict] of OpenAI-style messages:
  {"role":"user","content":...}
  {"role":"assistant","content":...,"reasoning":...,"tool_calls":[...]}
  {"role":"tool","tool_call_id":...,"name":...,"content":...}

The `messages` business table is still the display source of truth for the frontend;
this store is what the agent replays/continues from.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import aiosqlite

from .config import DB_PATH

_CREATE = """
CREATE TABLE IF NOT EXISTS session_history (
    session_id TEXT PRIMARY KEY,
    history    TEXT NOT NULL DEFAULT '[]',
    updated_at INTEGER NOT NULL DEFAULT 0
);
"""

_locks: dict[str, asyncio.Lock] = {}


def _lock(session_id: str) -> asyncio.Lock:
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


class HistoryStore:
    async def init(self) -> None:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.executescript(_CREATE)
            await conn.commit()

    async def get(self, session_id: str) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT history FROM session_history WHERE session_id=?", (session_id,)
            )
            row = await cur.fetchone()
        if not row:
            return []
        try:
            data = json.loads(row["history"] or "[]")
            return data if isinstance(data, list) else []
        except Exception:
            return []

    async def set(self, session_id: str, history: list[dict]) -> None:
        payload = json.dumps(history or [], ensure_ascii=False)
        async with _lock(session_id):
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    "INSERT INTO session_history(session_id,history,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET history=excluded.history, updated_at=excluded.updated_at",
                    (session_id, payload, _now_ms()),
                )
                await conn.commit()

    async def append(self, session_id: str, messages: list[dict]) -> list[dict]:
        """Append messages atomically and return the new full history."""
        async with _lock(session_id):
            async with aiosqlite.connect(DB_PATH) as conn:
                conn.row_factory = aiosqlite.Row
                cur = await conn.execute(
                    "SELECT history FROM session_history WHERE session_id=?", (session_id,)
                )
                row = await cur.fetchone()
                try:
                    history = json.loads(row["history"] or "[]") if row else []
                except Exception:
                    history = []
                if not isinstance(history, list):
                    history = []
                history.extend(messages or [])
                await conn.execute(
                    "INSERT INTO session_history(session_id,history,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET history=excluded.history, updated_at=excluded.updated_at",
                    (session_id, json.dumps(history, ensure_ascii=False), _now_ms()),
                )
                await conn.commit()
                return history

    async def delete(self, session_id: str) -> None:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("DELETE FROM session_history WHERE session_id=?", (session_id,))
            await conn.commit()

    async def exists(self, session_id: str) -> bool:
        async with aiosqlite.connect(DB_PATH) as conn:
            cur = await conn.execute("SELECT 1 FROM session_history WHERE session_id=?", (session_id,))
            return (await cur.fetchone()) is not None


_history_store: Optional[HistoryStore] = None


def get_history_store() -> HistoryStore:
    global _history_store
    if _history_store is None:
        _history_store = HistoryStore()
    return _history_store


def set_history_store(store: Optional[HistoryStore]) -> None:
    global _history_store
    _history_store = store
