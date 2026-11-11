"""GA 会话结果的持久化：关闭后仍可读。

**为什么需要**：2026-11-11 真机事故——模型调
``ga_result(close=true, max_chars=8000)``，全文 13223 字符，读到 8000 就被截断，
而 ``close=true`` 同时把内存里的会话销毁了。之后想补读全文，只拿到
``KeyError: 未知的 GA 会话``。**结果从未落过盘，关闭即永久丢失。**

本表与 ``subagent_runs`` 分开，理由是两者语义不同：

- ``subagent_runs`` 是子代理的执行记录（每个子代理一条，跨轮次可查询）；
- ``ga_runs`` 是 GA 会话的**结果快照**（用于关闭后补读，也便于排障）。

两者共用同一份 owner 作用域（user / session / generation），便于统一清理。
"""
from __future__ import annotations

import time
from typing import Any, Optional

import aiosqlite

from ..config import DB_PATH

_TABLE = """
CREATE TABLE IF NOT EXISTS ga_runs (
    agent_id       TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL DEFAULT '',
    session_id     TEXT NOT NULL DEFAULT '',
    generation_id  TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT '',
    result_text    TEXT NOT NULL DEFAULT '',
    full_text      TEXT NOT NULL DEFAULT '',
    error          TEXT NOT NULL DEFAULT '',
    stop_reason    TEXT NOT NULL DEFAULT '',
    tool_calls     INTEGER NOT NULL DEFAULT 0,
    prompts        INTEGER NOT NULL DEFAULT 0,
    updated_at     INTEGER NOT NULL DEFAULT 0
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_ga_runs_owner
    ON ga_runs(user_id, session_id, generation_id, updated_at);
"""


async def init_tables() -> None:
    """幂等建表；与其它侧链表一起在启动时调用。"""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.executescript(_TABLE)
        await conn.executescript(_INDEXES)
        await conn.commit()


async def save_result(
    agent_id: str,
    *,
    user_id: str = "",
    session_id: str = "",
    generation_id: str = "",
    title: str = "",
    status: str = "",
    result_text: str = "",
    full_text: str = "",
    error: str = "",
    stop_reason: str = "",
    tool_calls: int = 0,
    prompts: int = 0,
) -> None:
    """写入 / 覆盖一条 GA 结果快照。``agent_id`` 是主键，重复调用即更新。"""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO ga_runs(agent_id,user_id,session_id,generation_id,title,status,"
            "result_text,full_text,error,stop_reason,tool_calls,prompts,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "user_id=excluded.user_id, session_id=excluded.session_id, "
            "generation_id=excluded.generation_id, title=excluded.title, "
            "status=excluded.status, result_text=excluded.result_text, "
            "full_text=excluded.full_text, error=excluded.error, "
            "stop_reason=excluded.stop_reason, tool_calls=excluded.tool_calls, "
            "prompts=excluded.prompts, updated_at=excluded.updated_at",
            (
                agent_id, user_id, session_id, generation_id, title, status,
                result_text, full_text, error, stop_reason,
                int(tool_calls), int(prompts), int(time.time() * 1000),
            ),
        )
        await conn.commit()


async def load_result(agent_id: str) -> Optional[dict[str, Any]]:
    """按 agent_id 读回快照；不存在返回 ``None``。"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM ga_runs WHERE agent_id=?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def delete_for_generation(user_id: str, session_id: str, generation_id: str) -> int:
    """清理某个 generation 的全部快照（预留：generation 回收时调用）。"""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "DELETE FROM ga_runs WHERE user_id=? AND session_id=? AND generation_id=?",
            (user_id, session_id, generation_id),
        )
        await conn.commit()
        return cur.rowcount or 0