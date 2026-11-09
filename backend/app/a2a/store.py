"""SQLite persistence for per-user A2A agents, tokens and tasks."""
from __future__ import annotations

import json
import secrets
import time
from typing import Any, Optional

import aiosqlite

from ..config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS a2a_agents (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL UNIQUE,
    agent_id    TEXT NOT NULL UNIQUE,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS a2a_tokens (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    token_hash   TEXT NOT NULL UNIQUE,
    prefix       TEXT NOT NULL DEFAULT '',
    scopes       TEXT NOT NULL DEFAULT '["chat"]',
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER,
    expires_at   INTEGER,
    revoked_at   INTEGER
);
CREATE TABLE IF NOT EXISTS a2a_tasks (
    task_id       TEXT PRIMARY KEY,
    context_id    TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    state         TEXT NOT NULL,
    artifacts     TEXT NOT NULL DEFAULT '[]',
    error         TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_a2a_tokens_hash ON a2a_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_a2a_tokens_user ON a2a_tokens(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_a2a_tasks_scope
    ON a2a_tasks(user_id, agent_id, context_id, created_at);
CREATE INDEX IF NOT EXISTS idx_a2a_tasks_message
    ON a2a_tasks(agent_id, context_id, message_id);
CREATE INDEX IF NOT EXISTS idx_a2a_tasks_state ON a2a_tasks(state);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


async def init_tables() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.executescript(_SCHEMA)
        await conn.execute(
            "UPDATE a2a_tasks SET state='TASK_STATE_FAILED', error='server restart', updated_at=? "
            "WHERE state IN ('TASK_STATE_SUBMITTED','TASK_STATE_WORKING')",
            (now_ms(),),
        )
        await conn.commit()


def _row_to_agent(row: aiosqlite.Row) -> dict[str, Any]:
    return dict(row)


def _row_to_token(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["scopes"] = json.loads(d.get("scopes") or "[]")
    except Exception:
        d["scopes"] = []
    return d


def _row_to_task(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["artifacts"] = json.loads(d.get("artifacts") or "[]")
    except Exception:
        d["artifacts"] = []
    return d


async def ensure_agent(user_id: str) -> dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000;")
        cur = await conn.execute("SELECT * FROM a2a_agents WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row is not None:
            return _row_to_agent(row)
        agent_id = "ha_" + secrets.token_hex(8)
        try:
            await conn.execute(
                "INSERT INTO a2a_agents(id,user_id,agent_id,created_at) VALUES(?,?,?,?)",
                (new_id("a2a"), user_id, agent_id, now_ms()),
            )
            await conn.commit()
        except aiosqlite.IntegrityError:
            await conn.rollback()
        cur = await conn.execute("SELECT * FROM a2a_agents WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("failed to create A2A agent")
    return _row_to_agent(row)


async def get_agent(user_id: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM a2a_agents WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    return _row_to_agent(row) if row else None


async def get_agent_by_agent_id(agent_id: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM a2a_agents WHERE agent_id=?", (agent_id,))
        row = await cur.fetchone()
    return _row_to_agent(row) if row else None

async def create_token(*, user_id: str, agent_id: str, name: str, token_hash: str,
                       prefix: str, scopes: list[str], expires_at: Optional[int]) -> dict[str, Any]:
    tid = new_id("a2at")
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "INSERT INTO a2a_tokens(id,user_id,agent_id,name,token_hash,prefix,scopes,"
            "created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (tid, user_id, agent_id, name, token_hash, prefix,
             json.dumps(scopes, ensure_ascii=False), ts, expires_at),
        )
        await conn.commit()
        cur = await conn.execute("SELECT * FROM a2a_tokens WHERE id=?", (tid,))
        row = await cur.fetchone()
    return _row_to_token(row)


async def list_tokens(user_id: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM a2a_tokens WHERE user_id=? ORDER BY created_at DESC", (user_id,)
        )
        rows = await cur.fetchall()
    return [_row_to_token(r) for r in rows]


async def find_token_by_hash(token_hash: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM a2a_tokens WHERE token_hash=?", (token_hash,))
        row = await cur.fetchone()
    return _row_to_token(row) if row else None


async def touch_token(token_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE a2a_tokens SET last_used_at=? WHERE id=?", (now_ms(), token_id))
        await conn.commit()


async def revoke_token(user_id: str, token_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE a2a_tokens SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
            (now_ms(), token_id, user_id),
        )
        await conn.commit()
        return cur.rowcount > 0

async def create_task(*, task_id: str, context_id: str, user_id: str, agent_id: str,
                      session_id: str, message_id: str, generation_id: str,
                      state: str = "TASK_STATE_SUBMITTED") -> dict[str, Any]:
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "INSERT INTO a2a_tasks(task_id,context_id,user_id,agent_id,session_id,message_id,"
            "generation_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, context_id, user_id, agent_id, session_id, message_id,
             generation_id, state, ts, ts),
        )
        await conn.commit()
        cur = await conn.execute("SELECT * FROM a2a_tasks WHERE task_id=?", (task_id,))
        row = await cur.fetchone()
    return _row_to_task(row)


async def get_task(user_id: str, agent_id: str, task_id: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM a2a_tasks WHERE task_id=? AND user_id=? AND agent_id=?",
            (task_id, user_id, agent_id),
        )
        row = await cur.fetchone()
    return _row_to_task(row) if row else None


async def find_task_by_message(agent_id: str, context_id: str, message_id: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM a2a_tasks WHERE agent_id=? AND context_id=? AND message_id=? "
            "ORDER BY created_at ASC LIMIT 1",
            (agent_id, context_id, message_id),
        )
        row = await cur.fetchone()
    return _row_to_task(row) if row else None


async def update_task(task_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    allowed = {"state", "artifacts", "error", "message_id", "generation_id"}
    sets, vals = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "artifacts" and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        sets.append(f"{key}=?")
        vals.append(value)
    if not sets:
        return None
    sets.append("updated_at=?")
    vals.append(now_ms())
    vals.append(task_id)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(f"UPDATE a2a_tasks SET {', '.join(sets)} WHERE task_id=?", vals)
        await conn.commit()
        cur = await conn.execute("SELECT * FROM a2a_tasks WHERE task_id=?", (task_id,))
        row = await cur.fetchone()
    return _row_to_task(row) if row else None


async def list_tasks(user_id: str, agent_id: str, *, context_id: str = "",
                     state: str = "", limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM a2a_tasks WHERE user_id=? AND agent_id=?"
    vals: list[Any] = [user_id, agent_id]
    if context_id:
        sql += " AND context_id=?"
        vals.append(context_id)
    if state:
        sql += " AND state=?"
        vals.append(state)
    sql += " ORDER BY created_at DESC LIMIT ?"
    vals.append(max(1, min(100, int(limit))))
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, vals)
        rows = await cur.fetchall()
    return [_row_to_task(r) for r in rows]


async def find_task_by_message_any(agent_id: str, message_id: str) -> Optional[dict[str, Any]]:
    """Idempotency lookup when the caller omitted contextId."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM a2a_tasks WHERE agent_id=? AND message_id=? "
            "ORDER BY created_at ASC LIMIT 1",
            (agent_id, message_id),
        )
        row = await cur.fetchone()
    return _row_to_task(row) if row else None
