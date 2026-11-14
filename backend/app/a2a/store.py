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
    enabled     INTEGER NOT NULL DEFAULT 0,
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
    tool_policy  TEXT NOT NULL DEFAULT 'full',
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
    tool_policy   TEXT NOT NULL DEFAULT 'full',
    state         TEXT NOT NULL,
    artifacts     TEXT NOT NULL DEFAULT '[]',
    error         TEXT NOT NULL DEFAULT '',
    snapshot      TEXT NOT NULL DEFAULT '{}',
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
CREATE TABLE IF NOT EXISTS a2a_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    user_id     TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    token_id    TEXT NOT NULL DEFAULT '',
    method      TEXT NOT NULL,
    task_id     TEXT NOT NULL DEFAULT '',
    tool_policy TEXT NOT NULL DEFAULT '',
    outcome     TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    latency_ms  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_a2a_audit_user ON a2a_audit(user_id, ts DESC);
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
        # 老库升级：CREATE TABLE IF NOT EXISTS 不会补列，必须显式 ALTER。
        # 用户级开关默认关（含老用户迁移）：站长先开实例级 env，用户再自己点开。
        cur = await conn.execute("PRAGMA table_info(a2a_agents)")
        agent_cols = {r[1] for r in await cur.fetchall()}
        if "enabled" not in agent_cols:
            await conn.execute(
                "ALTER TABLE a2a_agents ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0"
            )
        for table in ("a2a_tokens", "a2a_tasks"):
            cur = await conn.execute(f"PRAGMA table_info({table})")
            cols = {r[1] for r in await cur.fetchall()}
            if "tool_policy" not in cols:
                await conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN tool_policy TEXT NOT NULL DEFAULT 'full'"
                )
        # 中断现场（2026-11-14）。重启后不能只说「server restart」——
        # 调用方需要知道「跑到第几步、最后在做什么、已生成多少」才能判断
        # 该重试还是该放弃。这一列存的是**可序列化的进度**，不是活对象。
        cur = await conn.execute("PRAGMA table_info(a2a_tasks)")
        if "snapshot" not in {r[1] for r in await cur.fetchall()}:
            await conn.execute(
                "ALTER TABLE a2a_tasks ADD COLUMN snapshot TEXT NOT NULL DEFAULT '{}'"
            )
        await conn.commit()
    # 收敛上一进程遗留的未完成任务。放在事务外：它要先读 snapshot 再拼说明。
    await mark_interrupted_tasks("server restart")


def _row_to_agent(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["enabled"] = bool(d.get("enabled"))
    return d


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
                       prefix: str, scopes: list[str], expires_at: Optional[int],
                       tool_policy: str = "full") -> dict[str, Any]:
    tid = new_id("a2at")
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "INSERT INTO a2a_tokens(id,user_id,agent_id,name,token_hash,prefix,scopes,"
            "tool_policy,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (tid, user_id, agent_id, name, token_hash, prefix,
             json.dumps(scopes, ensure_ascii=False), tool_policy, ts, expires_at),
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
                      state: str = "TASK_STATE_SUBMITTED",
                      tool_policy: str = "full") -> dict[str, Any]:
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "INSERT INTO a2a_tasks(task_id,context_id,user_id,agent_id,session_id,message_id,"
            "generation_id,tool_policy,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, context_id, user_id, agent_id, session_id, message_id,
             generation_id, tool_policy, state, ts, ts),
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


async def update_snapshot(task_id: str, *, steps: int = 0, tools_run: int = 0,
                          last_tool: str = "", partial_chars: int = 0) -> None:
    """把「跑到哪了」写进 DB。**这是重启后唯一能说清情况的依据**。

    为什么存这几个字段，而不是存整条事件流：
      - ``steps``        跑了几个模型轮次（决定「重跑要多少钱」）
      - ``tools_run``    成功执行了几个工具（决定「有多少副作用已经发生了」）
      - ``last_tool``    最后一个工具名（告诉人「断在哪一步」）
      - ``partial_chars`` 已经产出的正文字数（告诉人「已有多少可救」）

    不存每一条文本增量：正文本身已经在 ``messages`` 表里按 250ms 落了库，
    这里重复存只会让表膨胀，且重启后没有额外价值。

    **写得要便宜**：这个函数在工具边界被调用，不能在热路径上做重活。
    单条 UPDATE。
    """
    snap = {
        "steps": int(steps),
        "tools_run": int(tools_run),
        "last_tool": str(last_tool or ""),
        "partial_chars": int(partial_chars),
        "at": now_ms(),
    }
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE a2a_tasks SET snapshot=?, updated_at=? WHERE task_id=?",
            (json.dumps(snap, ensure_ascii=False), now_ms(), task_id),
        )
        await conn.commit()


def _describe_interruption(snap: dict[str, Any], reason: str) -> str:
    """把现场翻成一句人话。没有现场时**必须如实说没有**，不许编。"""
    if not snap:
        return (f"任务被中断（{reason}）：没有进展记录。"
                "无法确定已执行到哪一步，建议重新提交；"
                "如果上次可能已产生写入，请先自行核对结果。")
    steps = int(snap.get("steps") or 0)
    tools = int(snap.get("tools_run") or 0)
    last = str(snap.get("last_tool") or "")
    chars = int(snap.get("partial_chars") or 0)
    parts = [f"任务被中断（{reason}）：第 {steps} 步"]
    if last:
        parts.append(f"，最后执行的是 {last}")
    parts.append(f"，已成功执行 {tools} 个工具，已生成 {chars} 字")
    if tools:
        parts.append("。注意：中断前执行的工具可能已经产生了实际影响"
                     "（文件写入、命令执行），重新提交前请先核对")
    parts.append("。可以用同一 contextId 重新提交以继续。")
    return "".join(parts)


async def mark_interrupted_tasks(reason: str = "server restart") -> int:
    """把上一进程遗留的未完成任务收敛为 FAILED，**并写清中断现场**。

    与「一律写 server restart」的差别：调用方据此能判断该重试、该核对、
    还是该放弃。没有信息的中断说明等于让调用方自己去猜。

    只动 SUBMITTED / WORKING：已终态的任务不属于「被中断」。
    返回受影响行数（测试与启动日志用）。
    """
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000;")
        cur = await conn.execute(
            "SELECT task_id, snapshot FROM a2a_tasks "
            "WHERE state IN ('TASK_STATE_SUBMITTED','TASK_STATE_WORKING')"
        )
        rows = await cur.fetchall()
        if not rows:
            return 0
        updates: list[tuple[str, int, str]] = []
        for row in rows:
            try:
                snap = json.loads(row["snapshot"] or "{}")
            except (TypeError, ValueError):
                snap = {}
            updates.append((_describe_interruption(snap, reason), ts, row["task_id"]))
        await conn.executemany(
            "UPDATE a2a_tasks SET state='TASK_STATE_FAILED', error=?, updated_at=? "
            "WHERE task_id=?",
            updates,
        )
        await conn.commit()
    return len(updates)


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
_ACTIVE_STATES = ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING")
AUDIT_MAX_ROWS = 20000


async def count_active(user_id: str, exclude_task_id: str = "") -> tuple[int, int]:
    """返回 (该用户活跃任务数, 全局活跃任务数)，供并发配额判定。"""
    placeholders = ",".join("?" * len(_ACTIVE_STATES))
    sql = f"SELECT COUNT(*) FROM a2a_tasks WHERE state IN ({placeholders})"
    vals: list[Any] = list(_ACTIVE_STATES)
    if exclude_task_id:
        sql += " AND task_id<>?"
        vals.append(exclude_task_id)
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        cur = await conn.execute(sql, vals)
        total = int((await cur.fetchone())[0])
        cur = await conn.execute(sql + " AND user_id=?", vals + [user_id])
        mine = int((await cur.fetchone())[0])
    return mine, total


async def record_audit(*, user_id: str, agent_id: str, method: str, outcome: str,
                       token_id: str = "", task_id: str = "", tool_policy: str = "",
                       detail: str = "", latency_ms: int = 0) -> None:
    """一次 JSON-RPC 调用一行。审计失败不能影响主链路。"""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute(
            "INSERT INTO a2a_audit(ts,user_id,agent_id,token_id,method,task_id,"
            "tool_policy,outcome,detail,latency_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (now_ms(), user_id, agent_id, token_id, method, task_id, tool_policy,
             outcome, str(detail)[:500], int(latency_ms)),
        )
        cur = await conn.execute("SELECT MAX(id) FROM a2a_audit")
        max_id = int((await cur.fetchone())[0] or 0)
        if max_id > AUDIT_MAX_ROWS:
            await conn.execute("DELETE FROM a2a_audit WHERE id<=?", (max_id - AUDIT_MAX_ROWS,))
        await conn.commit()


async def list_audit(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM a2a_audit WHERE user_id=? ORDER BY ts DESC, id DESC LIMIT ?",
            (user_id, max(1, min(500, int(limit)))),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def set_agent_enabled(user_id: str, enabled: bool) -> Optional[dict[str, Any]]:
    """用户级开关：立刻生效（每次请求现查，不走缓存）。"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute(
            "UPDATE a2a_agents SET enabled=? WHERE user_id=?",
            (1 if enabled else 0, user_id),
        )
        await conn.commit()
        cur = await conn.execute("SELECT * FROM a2a_agents WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    return _row_to_agent(row) if row else None
