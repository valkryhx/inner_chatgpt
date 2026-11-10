"""Owner-scoped sidechain persistence for subagent runs.

Subagent messages never enter session_history. Every run operation accepts an
owner scope; single-argument forms remain compatibility wrappers for old tests.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import aiosqlite

from ..config import DB_PATH

_BASE_TABLES = """
CREATE TABLE IF NOT EXISTS subagent_runs (
    run_id          TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    message_id      TEXT NOT NULL DEFAULT '',
    parent_agent_id TEXT NOT NULL DEFAULT 'root',
    agent_id        TEXT NOT NULL,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'general',
    status          TEXT NOT NULL DEFAULT 'starting',
    task_message    TEXT NOT NULL DEFAULT '',
    result_text     TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    turns           INTEGER NOT NULL DEFAULT 0,
    started_at      INTEGER NOT NULL DEFAULT 0,
    finished_at     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS subagent_messages (
    run_id        TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    message       TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS subagent_notifications (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    generation_id TEXT NOT NULL DEFAULT '',
    agent_name    TEXT NOT NULL,
    status        TEXT NOT NULL,
    preview       TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    acked_at      INTEGER
);
"""
_SCOPE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_subruns_scope
    ON subagent_runs(user_id, session_id, generation_id, team_id, started_at);
CREATE INDEX IF NOT EXISTS idx_submsg_scope
    ON subagent_messages(user_id, session_id, generation_id, team_id, run_id, seq);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


async def init_tables() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        # Add columns before creating scope indexes.  Legacy tables do not have
        # generation_id/team_id/user_id yet, and an index referencing missing
        # columns would abort startup.
        await conn.executescript(_BASE_TABLES)
        for table, cols in (
            ("subagent_runs", (("generation_id", "TEXT NOT NULL DEFAULT ''"),
                               ("team_id", "TEXT NOT NULL DEFAULT ''"))),
            ("subagent_messages", (("user_id", "TEXT NOT NULL DEFAULT ''"),
                                   ("session_id", "TEXT NOT NULL DEFAULT ''"),
                                   ("generation_id", "TEXT NOT NULL DEFAULT ''"),
                                   ("team_id", "TEXT NOT NULL DEFAULT ''"))),
        ):
            cur = await conn.execute(f"PRAGMA table_info({table})")
            existing = {r[1] for r in await cur.fetchall()}
            for col, decl in cols:
                if col not in existing:
                    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        # Backfill message ownership from the authoritative parent run.
        await conn.execute(
            "UPDATE subagent_messages SET "
            "user_id=COALESCE((SELECT user_id FROM subagent_runs r WHERE r.run_id=subagent_messages.run_id),''), "
            "session_id=COALESCE((SELECT session_id FROM subagent_runs r WHERE r.run_id=subagent_messages.run_id),''), "
            "generation_id=COALESCE((SELECT generation_id FROM subagent_runs r WHERE r.run_id=subagent_messages.run_id),''), "
            "team_id=COALESCE((SELECT team_id FROM subagent_runs r WHERE r.run_id=subagent_messages.run_id),'') "
            "WHERE user_id='' OR session_id='' OR generation_id='' OR team_id=''"
        )
        await conn.executescript(_SCOPE_INDEXES)
        await conn.commit()


async def create_run(*, run_id: str, ctx_user_id: str, session_id: str, message_id: str,
                     parent_agent_id: str, agent_id: str, name: str, role: str,
                     task_message: str, status: str = "running",
                     generation_id: str = "", team_id: str = "") -> dict:
    started = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO subagent_runs(run_id,user_id,session_id,generation_id,team_id,message_id,"
            "parent_agent_id,agent_id,name,role,status,task_message,started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, ctx_user_id, session_id, generation_id, team_id, message_id,
             parent_agent_id, agent_id, name, role, status, task_message, started),
        )
        await conn.commit()
    return {"run_id": run_id, "user_id": ctx_user_id, "session_id": session_id,
            "generation_id": generation_id, "team_id": team_id, "message_id": message_id,
            "parent_agent_id": parent_agent_id, "agent_id": agent_id, "name": name,
            "role": role, "status": status, "task_message": task_message,
            "result_text": "", "error": "", "turns": 0,
            "started_at": started, "finished_at": 0}


def _scope_sql(base: str, run_id: str, user_id: str, session_id: str,
               generation_id: str, team_id: str) -> tuple[str, list[Any]]:
    sql = base
    vals: list[Any] = [run_id]
    for col, value in (("user_id", user_id), ("session_id", session_id),
                       ("generation_id", generation_id), ("team_id", team_id)):
        if value:
            sql += f" AND {col}=?"
            vals.append(value)
    return sql, vals


async def set_status(run_id: str, status: str, *, user_id: str = "", session_id: str = "",
                     generation_id: str = "", team_id: str = "") -> bool:
    sql, vals = _scope_sql("UPDATE subagent_runs SET status=? WHERE run_id=?", run_id,
                           user_id, session_id, generation_id, team_id)
    vals.insert(0, status)
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(sql, vals)
        await conn.commit()
        return (cur.rowcount or 0) == 1


async def finish_run(run_id: str, *, status: str, result_text: str = "", turns: int = 0,
                     error: str = "", user_id: str = "", session_id: str = "",
                     generation_id: str = "", team_id: str = "") -> bool:
    sql, vals = _scope_sql(
        "UPDATE subagent_runs SET status=?, result_text=?, turns=?, error=?, "
        "finished_at=? WHERE run_id=?", run_id, user_id, session_id, generation_id, team_id
    )
    vals[:0] = [status, result_text, int(turns), error, now_ms()]
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(sql, vals)
        await conn.commit()
        return (cur.rowcount or 0) == 1


async def append_sidechain(run_id: str, history: list[dict], *, start_seq: int = 0,
                           user_id: str = "", session_id: str = "",
                           generation_id: str = "", team_id: str = "") -> int:
    rows = history[start_seq:]
    if not rows:
        return start_seq
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        if user_id or session_id or generation_id or team_id:
            cur = await conn.execute(
                "SELECT 1 FROM subagent_runs WHERE run_id=? AND user_id=? AND session_id=? "
                "AND generation_id=? AND team_id=?",
                (run_id, user_id, session_id, generation_id, team_id),
            )
            if await cur.fetchone() is None:
                return start_seq
        await conn.executemany(
            "INSERT OR REPLACE INTO subagent_messages(run_id,seq,user_id,session_id,"
            "generation_id,team_id,message,created_at) VALUES(?,?,?,?,?,?,?,?)",
            [(run_id, start_seq + i, user_id, session_id, generation_id, team_id,
              json.dumps(m, ensure_ascii=False), ts) for i, m in enumerate(rows)],
        )
        await conn.commit()
    return start_seq + len(rows)


async def load_sidechain(run_id: str, *, user_id: str = "", session_id: str = "",
                         generation_id: str = "", team_id: str = "") -> list[dict]:
    sql = "SELECT message FROM subagent_messages WHERE run_id=?"
    vals: list[Any] = [run_id]
    for col, value in (("user_id", user_id), ("session_id", session_id),
                       ("generation_id", generation_id), ("team_id", team_id)):
        if value:
            sql += f" AND {col}=?"
            vals.append(value)
    sql += " ORDER BY seq ASC"
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, vals)
        rows = await cur.fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["message"]))
        except Exception:
            continue
    return out


async def get_run(run_id: str, *, user_id: str = "", session_id: str = "",
                  generation_id: str = "", team_id: str = "") -> Optional[dict]:
    sql, vals = _scope_sql("SELECT * FROM subagent_runs WHERE run_id=?", run_id,
                           user_id, session_id, generation_id, team_id)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, vals)
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_runs(session_id: str, user_id: str, *, message_id: str = "",
                    generation_id: str = "", team_id: str = "") -> list[dict]:
    sql = "SELECT * FROM subagent_runs WHERE session_id=? AND user_id=?"
    params: list[Any] = [session_id, user_id]
    if message_id:
        sql += " AND message_id=?"
        params.append(message_id)
    if generation_id:
        sql += " AND generation_id=?"
        params.append(generation_id)
    if team_id:
        sql += " AND team_id=?"
        params.append(team_id)
    sql += " ORDER BY started_at ASC"
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_stale_running_as_errored(reason: str = "restarted") -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE subagent_runs SET status='errored', error=?, finished_at=? "
            "WHERE status IN ('starting','running')",
            (f"interrupted: {reason}", now_ms()),
        )
        n = cur.rowcount or 0
        await conn.commit()
    return n

async def add_notification(*, nid: str, user_id: str, session_id: str,
                           generation_id: str, agent_name: str, status: str,
                           preview: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO subagent_notifications"
            "(id,user_id,session_id,generation_id,agent_name,status,preview,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (nid, user_id, session_id, generation_id, agent_name, status, preview, now_ms()),
        )
        await conn.commit()


async def list_pending_notifications(user_id: str, session_id: str,
                                     limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM subagent_notifications"
            " WHERE user_id=? AND session_id=? AND acked_at IS NULL"
            " ORDER BY created_at ASC LIMIT ?",
            (user_id, session_id, max(1, min(int(limit), 100))),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def ack_notifications_db(ids: list[str]) -> int:
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            f"UPDATE subagent_notifications SET acked_at=? WHERE id IN ({placeholders})",
            (now_ms(), *ids),
        )
        await conn.commit()
        return cur.rowcount

async def get_latest_run_by_name(user_id: str, session_id: str, name: str) -> dict | None:
    """按名字取本会话最近一次 run（跨 generation 回退用）。"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM subagent_runs WHERE user_id=? AND session_id=? AND name=?"
            " ORDER BY started_at DESC LIMIT 1",
            (user_id, session_id, name),
        )
        row = await cur.fetchone()
    return dict(row) if row else None
