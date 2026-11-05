"""侧链持久化：subagent_runs + subagent_messages。

铁律（见设计文档 §4.2）：子代理消息**绝不**写进 session_history。
主对话的权威回放源只有 session_history；子代理过程单独两张表，按需展示。
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import aiosqlite

from ..config import DB_PATH

_CREATE = """
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
CREATE INDEX IF NOT EXISTS idx_subruns_session ON subagent_runs(session_id, started_at);
CREATE INDEX IF NOT EXISTS idx_subruns_user ON subagent_runs(user_id, session_id);

CREATE TABLE IF NOT EXISTS subagent_messages (
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    message    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


async def init_tables() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.executescript(_CREATE)
        await conn.commit()


async def create_run(*, run_id: str, ctx_user_id: str, session_id: str, message_id: str,
                     parent_agent_id: str, agent_id: str, name: str, role: str,
                     task_message: str, status: str = "running") -> dict:
    started = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO subagent_runs(run_id,user_id,session_id,message_id,parent_agent_id,"
            "agent_id,name,role,status,task_message,started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, ctx_user_id, session_id, message_id, parent_agent_id, agent_id,
             name, role, status, task_message, started),
        )
        await conn.commit()
    return {
        "run_id": run_id, "user_id": ctx_user_id, "session_id": session_id,
        "message_id": message_id, "parent_agent_id": parent_agent_id,
        "agent_id": agent_id, "name": name, "role": role, "status": status,
        "task_message": task_message, "result_text": "", "error": "",
        "turns": 0, "started_at": started, "finished_at": 0,
    }


async def set_status(run_id: str, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE subagent_runs SET status=? WHERE run_id=?", (status, run_id))
        await conn.commit()


async def finish_run(run_id: str, *, status: str, result_text: str = "",
                     turns: int = 0, error: str = "") -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE subagent_runs SET status=?, result_text=?, turns=?, error=?, finished_at=? "
            "WHERE run_id=?",
            (status, result_text, int(turns), error, now_ms(), run_id),
        )
        await conn.commit()


async def append_sidechain(run_id: str, history: list[dict], *, start_seq: int = 0) -> int:
    """把 history 里从 start_seq 开始的新消息追加到侧链表，返回新的已写条数。

    只追加增量，避免整表重写（history 可能很长）。
    """
    rows = history[start_seq:]
    if not rows:
        return start_seq
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executemany(
            "INSERT OR REPLACE INTO subagent_messages(run_id,seq,message,created_at) VALUES(?,?,?,?)",
            [(run_id, start_seq + i, json.dumps(m, ensure_ascii=False), ts)
             for i, m in enumerate(rows)],
        )
        await conn.commit()
    return start_seq + len(rows)


async def load_sidechain(run_id: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT message FROM subagent_messages WHERE run_id=? ORDER BY seq ASC", (run_id,)
        )
        rows = await cur.fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            out.append(json.loads(r["message"]))
        except Exception:
            continue
    return out


async def get_run(run_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM subagent_runs WHERE run_id=?", (run_id,))
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_runs(session_id: str, user_id: str, *, message_id: str = "") -> list[dict]:
    """只返回本用户本会话的 run —— 所有权过滤不可省（对齐 Aivory）。"""
    sql = "SELECT * FROM subagent_runs WHERE session_id=? AND user_id=?"
    params: list[Any] = [session_id, user_id]
    if message_id:
        sql += " AND message_id=?"
        params.append(message_id)
    sql += " ORDER BY started_at ASC"
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_stale_running_as_errored(reason: str = "restarted") -> int:
    """进程启动时调用：把上次进程遗留的 running 状态收敛为 errored。"""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE subagent_runs SET status='errored', error=?, finished_at=? "
            "WHERE status IN ('starting','running')",
            (f"interrupted: {reason}", now_ms()),
        )
        n = cur.rowcount or 0
        await conn.commit()
    return n
