"""Owner-scoped persistent chat history.

One row per (user_id, session_id).  The user dimension is not decorative: every
read/write goes through an owner-scoped method.  Legacy call sites temporarily use
the single-argument compatibility methods, which are only safe when the caller has
already verified session ownership.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import aiosqlite

from .config import DB_PATH

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS session_history (
    user_id     TEXT NOT NULL DEFAULT '',
    session_id  TEXT NOT NULL,
    history     TEXT NOT NULL DEFAULT '[]',
    updated_at  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, session_id)
);
"""
_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_history_session_owner
    ON session_history(user_id, session_id);
"""

_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _lock(user_id: str, session_id: str) -> asyncio.Lock:
    key = (user_id, session_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _now_ms() -> int:
    return int(time.time() * 1000)


class HistoryStore:
    async def init(self) -> None:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA busy_timeout=5000;")
            # Create only the base table first.  Existing databases may have the
            # legacy session_id-only schema, so new indexes must not run before
            # the compatibility columns are present.
            await conn.executescript(_CREATE_TABLE)
            cur = await conn.execute("PRAGMA table_info(session_history)")
            cols = {row[1] for row in await cur.fetchall()}
            if "user_id" not in cols:
                # SQLite cannot add a composite PK in place: rebuild once.
                await conn.execute("ALTER TABLE session_history RENAME TO session_history_old")
                await conn.executescript(_CREATE_TABLE)
                await conn.execute(
                    "INSERT OR REPLACE INTO session_history(user_id, session_id, history, updated_at) "
                    "SELECT '', session_id, history, updated_at FROM session_history_old"
                )
                await conn.execute("DROP TABLE session_history_old")
            # Backfill ownership for the pre-multi-user history.  sessions is the
            # authoritative owner map; leave rows orphaned only when it is absent.
            cur = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
            )
            if await cur.fetchone() is not None:
                await conn.execute(
                    "UPDATE session_history SET user_id=COALESCE(("
                    "SELECT user_id FROM sessions WHERE sessions.id=session_history.session_id"
                    "), '') WHERE user_id='' OR user_id IS NULL"
                )
            await conn.executescript(_CREATE_INDEX)
            await conn.commit()

    async def get(self, user_id: str, session_id: str | None = None) -> list[dict]:
        """Read history.  Two-argument form is owner-scoped; one-argument form is
        a temporary legacy compatibility path with user_id=''."""
        if session_id is None:
            owner, sid = "", str(user_id)
        else:
            owner, sid = str(user_id), str(session_id)
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT history FROM session_history WHERE user_id=? AND session_id=?",
                (owner, sid),
            )
            row = await cur.fetchone()
        if not row:
            return []
        try:
            data = json.loads(row["history"] or "[]")
            return data if isinstance(data, list) else []
        except Exception:
            return []

    async def set(self, user_id: str, session_id_or_history, history: list[dict] | None = None) -> None:
        if history is None:
            owner, sid, payload = "", str(user_id), session_id_or_history
        else:
            owner, sid, payload = str(user_id), str(session_id_or_history), history
        text = json.dumps(payload or [], ensure_ascii=False)
        async with _lock(owner, sid):
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute("PRAGMA busy_timeout=5000;")
                await conn.execute(
                    "INSERT INTO session_history(user_id,session_id,history,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(user_id,session_id) DO UPDATE SET history=excluded.history, updated_at=excluded.updated_at",
                    (owner, sid, text, _now_ms()),
                )
                await conn.commit()

    async def append(self, user_id: str, session_id_or_messages, messages: list[dict] | None = None) -> list[dict]:
        """Atomically append messages.  Owner-scoped form: append(uid, sid, msgs)."""
        if messages is None:
            owner, sid, new_messages = "", str(user_id), session_id_or_messages
        else:
            owner, sid, new_messages = str(user_id), str(session_id_or_messages), messages
        async with _lock(owner, sid):
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute("PRAGMA busy_timeout=5000;")
                conn.row_factory = aiosqlite.Row
                cur = await conn.execute(
                    "SELECT history FROM session_history WHERE user_id=? AND session_id=?",
                    (owner, sid),
                )
                row = await cur.fetchone()
                try:
                    history = json.loads(row["history"] or "[]") if row else []
                except Exception:
                    history = []
                if not isinstance(history, list):
                    history = []
                history.extend(new_messages or [])
                await conn.execute(
                    "INSERT INTO session_history(user_id,session_id,history,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(user_id,session_id) DO UPDATE SET history=excluded.history, updated_at=excluded.updated_at",
                    (owner, sid, json.dumps(history, ensure_ascii=False), _now_ms()),
                )
                await conn.commit()
                return history

    async def delete(self, user_id: str, session_id: str | None = None) -> None:
        if session_id is None:
            owner, sid = "", str(user_id)
        else:
            owner, sid = str(user_id), str(session_id)
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "DELETE FROM session_history WHERE user_id=? AND session_id=?", (owner, sid)
            )
            await conn.commit()

    async def exists(self, user_id: str, session_id: str | None = None) -> bool:
        if session_id is None:
            owner, sid = "", str(user_id)
        else:
            owner, sid = str(user_id), str(session_id)
        async with aiosqlite.connect(DB_PATH) as conn:
            cur = await conn.execute(
                "SELECT 1 FROM session_history WHERE user_id=? AND session_id=?", (owner, sid)
            )
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
