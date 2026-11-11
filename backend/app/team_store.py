"""Persistent Agent Team facts: teams, tasks, dependencies, messages and events.

All task/message mutations are owner-scoped and CAS-based. This repository is
independent from subagent_runs: a team is durable collaboration state, while a
subagent run is one execution attempt.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any, Optional

import aiosqlite

from .config import DB_PATH
from .logging_setup import team_event
from .scope import TeamScope

TERMINAL_TASK_STATUSES = frozenset({"completed", "cancelled"})
TASK_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})
TEAM_STATUSES = frozenset({"active", "completed", "stopped", "failed", "stale"})

# 消息投递状态：queued -> delivered -> consumed；held/dropped 是非正常路径。
# 参考 CC319 的 receipt 状态机（m0188）与 GA 的 mailbox state 字段。
MESSAGE_STATES = frozenset({"queued", "delivered", "consumed", "held", "dropped"})

# lead 的多种写法都要落到同一个收件箱。模型会写 lead / root / main / team-lead，
# 内部 agent_id 却是 "root"；不归一化就会出现「发给 lead」和「root 收件箱」两个篮子。
LEAD_ALIASES = frozenset({"lead", "root", "main", "team-lead", "team_lead"})
LEAD_AGENT_ID = "root"
SUBAGENT_PREFIX = "sub:"

_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")


def normalize_agent_id(raw: str | None) -> str:
    """把任意写法的收件人归一到内部 agent_id。

    lead/root/main/team-lead -> "root"；poet / sub:poet / sub:poet_2 -> "sub:<name>"。
    空值与非法名字返回空串，由调用方决定是拒绝还是兜底——本函数不做策略判断。
    """
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if text in LEAD_ALIASES:
        return LEAD_AGENT_ID
    if text.startswith(SUBAGENT_PREFIX):
        name = text[len(SUBAGENT_PREFIX):]
    else:
        name = text
    # 去掉模型可能带上的路径/斜杠写法（/root/poet）
    name = name.replace(chr(92), "/").rstrip("/").split("/")[-1]
    return SUBAGENT_PREFIX + name if name else ""


def agent_name_of(agent_id: str) -> str:
    """内部 agent_id -> 裸名（用于 roster 查找与展示）。"""
    text = str(agent_id or "")
    return text[len(SUBAGENT_PREFIX):] if text.startswith(SUBAGENT_PREFIX) else text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_runs (
    team_id         TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    generation_id   TEXT NOT NULL,
    lead_agent_id   TEXT NOT NULL,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    closed_at       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_team_scope
    ON team_runs(user_id, session_id, generation_id, team_id);

CREATE TABLE IF NOT EXISTS team_tasks (
    task_id         TEXT PRIMARY KEY,
    team_id         TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    generation_id   TEXT NOT NULL,
    subject         TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    owner_agent_id  TEXT NOT NULL DEFAULT '',
    created_by      TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    version         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_team_tasks_scope
    ON team_tasks(user_id, session_id, generation_id, team_id, status);
CREATE INDEX IF NOT EXISTS idx_team_tasks_owner
    ON team_tasks(team_id, owner_agent_id);

CREATE TABLE IF NOT EXISTS team_task_deps (
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    generation_id   TEXT NOT NULL,
    team_id         TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    blocked_by      TEXT NOT NULL,
    PRIMARY KEY (team_id, task_id, blocked_by)
);
CREATE INDEX IF NOT EXISTS idx_team_deps_scope
    ON team_task_deps(user_id, session_id, generation_id, team_id);

CREATE TABLE IF NOT EXISTS team_messages (
    message_id          TEXT PRIMARY KEY,
    team_id             TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    generation_id       TEXT NOT NULL,
    sender_agent_id     TEXT NOT NULL,
    recipient_agent_id  TEXT NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'note',
    content             TEXT NOT NULL,
    created_at          INTEGER NOT NULL,
    delivered_at        INTEGER NOT NULL DEFAULT 0,
    read_at             INTEGER NOT NULL DEFAULT 0,
    state               TEXT NOT NULL DEFAULT 'queued'
);
CREATE INDEX IF NOT EXISTS idx_team_messages_scope
    ON team_messages(user_id, session_id, generation_id, team_id, created_at);
CREATE INDEX IF NOT EXISTS idx_team_messages_recipient
    ON team_messages(team_id, recipient_agent_id, read_at);

-- 名字钉住（CC319 的 sendMessagePins / GA 的 name_pins）：一个名字第一次送达时
-- 绑定到那个 agent。名字被复用后，旧名字的消息不能默默投给新 agent，否则就是串名。
CREATE TABLE IF NOT EXISTS team_name_pins (
    team_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    pinned_at   INTEGER NOT NULL,
    PRIMARY KEY (team_id, name)
);

CREATE TABLE IF NOT EXISTS team_events (
    team_id       TEXT NOT NULL,
    event_seq     INTEGER NOT NULL,
    user_id       TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    task_id       TEXT NOT NULL DEFAULT '',
    event_type    TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (team_id, event_seq)
);
CREATE INDEX IF NOT EXISTS idx_team_events_scope
    ON team_events(user_id, session_id, generation_id, team_id, event_seq);

CREATE TABLE IF NOT EXISTS session_generations (
    user_id       TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    started_at    INTEGER NOT NULL,
    finished_at   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, session_id, generation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_generation
    ON session_generations(user_id, session_id) WHERE status='active';
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


async def init_tables() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        await conn.executescript(_SCHEMA)
        # 旧库迁移：team_messages 增加投递状态列（幂等）。已有行按 read_at 回填，
        # 否则升级后所有历史消息都会被当成「未投递」重放一遍。
        try:
            await conn.execute(
                "ALTER TABLE team_messages ADD COLUMN state TEXT NOT NULL DEFAULT 'queued'")
            await conn.execute(
                "UPDATE team_messages SET state='consumed' WHERE read_at>0")
            await conn.execute(
                "UPDATE team_messages SET state='delivered' "
                "WHERE read_at=0 AND delivered_at>0")
        except Exception:
            pass
        await conn.commit()


def _scope_where(scope: TeamScope, alias: str = "") -> tuple[str, tuple[str, str, str, str]]:
    p = f"{alias}." if alias else ""
    return (
        f"{p}user_id=? AND {p}session_id=? AND {p}generation_id=? AND {p}team_id=?",
        scope.key(),
    )



# ---------------- generation lease ----------------
async def acquire_generation(user_id: str, session_id: str, generation_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        try:
            await conn.execute(
                "INSERT INTO session_generations(user_id,session_id,generation_id,status,started_at) "
                "VALUES(?,?,?,'active',?)",
                (user_id, session_id, generation_id, now_ms()),
            )
            await conn.commit()
            return True
        except aiosqlite.IntegrityError:
            await conn.rollback()
            return False


async def release_generation(user_id: str, session_id: str, generation_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE session_generations SET status='finished', finished_at=? "
            "WHERE user_id=? AND session_id=? AND generation_id=? AND status='active'",
            (now_ms(), user_id, session_id, generation_id),
        )
        await conn.commit()


async def get_active_generation(user_id: str, session_id: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT generation_id FROM session_generations "
            "WHERE user_id=? AND session_id=? AND status='active' LIMIT 1",
            (user_id, session_id),
        )
        row = await cur.fetchone()
    return str(row[0]) if row else None


async def converge_generation_facts(user_id: str, session_id: str, generation_id: str,
                                    *, status: str = "stale") -> dict[str, int]:
    """Close an active generation and return its Team facts to a safe state.

    Used both for explicit stop (status=stopped) and for a lease left behind
    when no in-process task exists anymore (status=stale).  In-progress tasks
    become pending with a new version so an old CAS holder cannot resurrect
    them.  The caller must still decide whether a live task may still write.
    """
    if status not in TEAM_STATUSES:
        status = "stale"
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute("BEGIN IMMEDIATE")
        try:
            gen_cur = await conn.execute(
                "UPDATE session_generations SET status=?,finished_at=? "
                "WHERE user_id=? AND session_id=? AND generation_id=? AND status='active'",
                (status, ts, user_id, session_id, generation_id),
            )
            task_cur = await conn.execute(
                "UPDATE team_tasks SET status='pending',owner_agent_id='',version=version+1,updated_at=? "
                "WHERE user_id=? AND session_id=? AND generation_id=? AND status='in_progress'",
                (ts, user_id, session_id, generation_id),
            )
            team_cur = await conn.execute(
                "UPDATE team_runs SET status=?,updated_at=?,closed_at=? "
                "WHERE user_id=? AND session_id=? AND generation_id=? AND status='active'",
                (status, ts, ts, user_id, session_id, generation_id),
            )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
    return {
        "generations": gen_cur.rowcount or 0,
        "tasks": task_cur.rowcount or 0,
        "teams": team_cur.rowcount or 0,
    }


# ---------------- team lifecycle ----------------
async def create_team(scope: TeamScope, *, name: str) -> dict[str, Any]:
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                "INSERT INTO team_runs(team_id,user_id,session_id,generation_id,lead_agent_id,name,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?, 'active', ?,?)",
                (scope.team_id, scope.user_id, scope.session_id, scope.generation_id,
                 scope.agent_id, name or "Agent Team", ts, ts),
            )
            await conn.execute(
                "INSERT INTO team_events(team_id,event_seq,user_id,session_id,generation_id,agent_id,event_type,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (scope.team_id, 1, scope.user_id, scope.session_id, scope.generation_id,
                 scope.agent_id, "team_start", json.dumps({"name": name}, ensure_ascii=False), ts),
            )
            from .team_resume import copy_resume_taskboard
            resumed = await copy_resume_taskboard(conn, scope, ts)
            if resumed is not None:
                await conn.execute(
                    "INSERT INTO team_events(team_id,event_seq,user_id,session_id,generation_id,agent_id,event_type,payload,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (scope.team_id, 2, scope.user_id, scope.session_id, scope.generation_id,
                     scope.agent_id, "team_resumed",
                     json.dumps(resumed, ensure_ascii=False), ts),
                )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
    return await get_team(scope) or {}


async def get_team(scope: TeamScope) -> Optional[dict[str, Any]]:
    where, args = _scope_where(scope)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(f"SELECT * FROM team_runs WHERE {where}", args)
        row = await cur.fetchone()
    return dict(row) if row else None


async def set_team_status(scope: TeamScope, status: str) -> bool:
    if status not in TEAM_STATUSES:
        raise ValueError(f"invalid team status: {status}")
    where, args = _scope_where(scope)
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            f"UPDATE team_runs SET status=?, updated_at=?, closed_at=? WHERE {where}",
            (status, now_ms(), now_ms() if status != "active" else 0, *args),
        )
        await conn.commit()
        return (cur.rowcount or 0) == 1


# ---------------- events ----------------
async def emit_event(scope: TeamScope, event_type: str, payload: dict[str, Any],
                     *, agent_id: str = "", task_id: str = "") -> dict[str, Any]:
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        # Reserve the write lock before allocating MAX+1.  A plain SELECT then
        # INSERT lets concurrent emitters observe the same sequence number.
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute(
                "SELECT COALESCE(MAX(event_seq),0)+1 FROM team_events WHERE team_id=?",
                (scope.team_id,),
            )
            seq = int((await cur.fetchone())[0])
            await conn.execute(
                "INSERT INTO team_events(team_id,event_seq,user_id,session_id,generation_id,agent_id,task_id,event_type,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (scope.team_id, seq, scope.user_id, scope.session_id, scope.generation_id,
                 agent_id or scope.agent_id, task_id, event_type,
                 json.dumps(payload or {}, ensure_ascii=False), ts),
            )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
    return {"team_id": scope.team_id, "event_seq": seq, "type": event_type,
            "agent_id": agent_id or scope.agent_id, "task_id": task_id, "payload": payload}


async def events_since(scope: TeamScope, since_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    where, args = _scope_where(scope)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"SELECT * FROM team_events WHERE {where} AND event_seq>? ORDER BY event_seq ASC LIMIT ?",
            (*args, max(0, int(since_seq)), max(1, min(int(limit), 1000))),
        )
        rows = await cur.fetchall()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload") or "{}")
        except Exception:
            d["payload"] = {}
        out.append(d)
    return out


async def create_task(scope: TeamScope, *, subject: str, description: str = "",
                      blocked_by: list[str] | None = None) -> dict[str, Any]:
    task_id = new_id("task")
    ts = now_ms()
    blocked = [str(x) for x in (blocked_by or []) if str(x)]
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        for dep in blocked:
            cur = await conn.execute(
                "SELECT 1 FROM team_tasks WHERE task_id=? AND user_id=? AND session_id=? "
                "AND generation_id=? AND team_id=?",
                (dep, *scope.key()),
            )
            if await cur.fetchone() is None:
                raise ValueError(f"blocked_by task not found in team: {dep}")
        await conn.execute(
            "INSERT INTO team_tasks(task_id,team_id,user_id,session_id,generation_id,subject,description,"
            "status,owner_agent_id,created_by,created_at,updated_at,version) "
            "VALUES(?,?,?,?,?,?,?, 'pending','',?,?,?,0)",
            (task_id, scope.team_id, scope.user_id, scope.session_id, scope.generation_id,
             subject.strip(), description or "", scope.agent_id, ts, ts),
        )
        await conn.executemany(
            "INSERT INTO team_task_deps(user_id,session_id,generation_id,team_id,task_id,blocked_by) "
            "VALUES(?,?,?,?,?,?)",
            [(scope.user_id, scope.session_id, scope.generation_id, scope.team_id, task_id, dep)
             for dep in blocked],
        )
        await conn.commit()
    task = await get_task(scope, task_id)
    assert task is not None
    await emit_event(scope, "task_created", {"task": task}, task_id=task_id)
    return task


async def get_task(scope: TeamScope, task_id: str) -> Optional[dict[str, Any]]:
    where, args = _scope_where(scope)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"SELECT * FROM team_tasks WHERE task_id=? AND {where}", (task_id, *args)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        dep_cur = await conn.execute(
            "SELECT blocked_by FROM team_task_deps WHERE task_id=? AND user_id=? AND session_id=? "
            "AND generation_id=? AND team_id=? ORDER BY blocked_by",
            (task_id, *scope.key()),
        )
        deps = [r[0] for r in await dep_cur.fetchall()]
    d = dict(row)
    d["blockedBy"] = deps
    return d


async def list_tasks(scope: TeamScope, *, status: str = "") -> list[dict[str, Any]]:
    where, args = _scope_where(scope)
    sql = f"SELECT * FROM team_tasks WHERE {where}"
    vals: list[Any] = list(args)
    if status:
        sql += " AND status=?"
        vals.append(status)
    sql += " ORDER BY created_at ASC"
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, vals)
        rows = await cur.fetchall()
        out = [dict(r) for r in rows]
        for task in out:
            dep_cur = await conn.execute(
                "SELECT blocked_by FROM team_task_deps WHERE task_id=? AND user_id=? AND session_id=? "
                "AND generation_id=? AND team_id=? ORDER BY blocked_by",
                (task["task_id"], *scope.key()),
            )
            task["blockedBy"] = [r[0] for r in await dep_cur.fetchall()]
    return out


async def _deps_complete(conn: aiosqlite.Connection, scope: TeamScope, task_id: str) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM team_task_deps d JOIN team_tasks t "
        "ON t.task_id=d.blocked_by AND t.user_id=d.user_id AND t.session_id=d.session_id "
        "AND t.generation_id=d.generation_id AND t.team_id=d.team_id "
        "WHERE d.task_id=? AND d.user_id=? AND d.session_id=? AND d.generation_id=? "
        "AND d.team_id=? AND t.status<>'completed' LIMIT 1",
        (task_id, *scope.key()),
    )
    return (await cur.fetchone()) is None


async def task_is_blocked(scope: TeamScope, task_id: str) -> bool:
    """True when ``task_id`` still has an incomplete ``blocked_by`` dependency.

    Public so callers can refuse *before* doing expensive work (e.g. spawning a
    teammate that would only be torn down again once the bind is rejected).
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        return not await _deps_complete(conn, scope, task_id)


async def claim_task(scope: TeamScope, task_id: str, *, expected_version: int) -> dict[str, Any]:
    where, args = _scope_where(scope)
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        if not await _deps_complete(conn, scope, task_id):
            return {"ok": False, "reason": "blocked"}
        cur = await conn.execute(
            f"UPDATE team_tasks SET status='in_progress',owner_agent_id=?,version=version+1,updated_at=? "
            f"WHERE task_id=? AND {where} AND status='pending' AND owner_agent_id='' AND version=?",
            (scope.agent_id, now_ms(), task_id, *args, int(expected_version)),
        )
        await conn.commit()
        ok = (cur.rowcount or 0) == 1
    if not ok:
        task = await get_task(scope, task_id)
        reason = "not_found" if task is None else "conflict"
        team_event("task_claim_failed", task_id=task_id, actor=scope.agent_id,
                   team_id=scope.team_id, reason=reason)
        return {"ok": False, "reason": reason}
    task = await get_task(scope, task_id)
    assert task is not None
    team_event("task_claimed", task_id=task_id, actor=scope.agent_id,
               team_id=scope.team_id, version=task.get("version"))
    await emit_event(scope, "task_updated", {"task": task, "action": "claim"}, task_id=task_id)
    return {"ok": True, "task": task}


async def complete_task(scope: TeamScope, task_id: str, *, expected_version: int) -> dict[str, Any]:
    where, args = _scope_where(scope)
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        cur = await conn.execute(
            f"UPDATE team_tasks SET status='completed',version=version+1,updated_at=? "
            f"WHERE task_id=? AND {where} AND status='in_progress' AND owner_agent_id=? AND version=?",
            (now_ms(), task_id, *args, scope.agent_id, int(expected_version)),
        )
        await conn.commit()
        ok = (cur.rowcount or 0) == 1
    if not ok:
        task = await get_task(scope, task_id)
        reason = "not_found" if task is None else "not_owner_or_conflict"
        # owner 不匹配是协作里最隐蔽的一类失败：任务明明在，但干活的人不是 owner。
        team_event("task_complete_failed", task_id=task_id, actor=scope.agent_id,
                   team_id=scope.team_id, reason=reason,
                   actual_owner=(task or {}).get("owner_agent_id", ""))
        return {"ok": False, "reason": reason}
    task = await get_task(scope, task_id)
    assert task is not None
    team_event("task_completed", task_id=task_id, actor=scope.agent_id,
               team_id=scope.team_id, version=task.get("version"))
    await emit_event(scope, "task_updated", {"task": task, "action": "complete"}, task_id=task_id)
    return {"ok": True, "task": task}


async def reclaim_task(scope: TeamScope, task_id: str, *, expected_version: int) -> dict[str, Any]:
    where, args = _scope_where(scope)
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        cur = await conn.execute(
            f"UPDATE team_tasks SET status='pending',owner_agent_id='',version=version+1,updated_at=? "
            f"WHERE task_id=? AND {where} AND status='in_progress' AND version=?",
            (now_ms(), task_id, *args, int(expected_version)),
        )
        await conn.commit()
        ok = (cur.rowcount or 0) == 1
    if not ok:
        task = await get_task(scope, task_id)
        return {"ok": False, "reason": "not_found" if task is None else "conflict"}
    task = await get_task(scope, task_id)
    assert task is not None
    await emit_event(scope, "task_updated", {"task": task, "action": "reclaim"}, task_id=task_id)
    return {"ok": True, "task": task}


async def bind_task_owner(scope: TeamScope, task_id: str, owner_agent_id: str, *,
                          expected_version: int) -> dict[str, Any]:
    """Assign a pending task to an agent, refusing one whose blockers are open.

    ``claim_task`` has always checked ``blocked_by``; this path did not, which
    made dependencies advisory: ``spawn_teammate(team_task_id=...)`` bound a
    blocked task and ran it early. Both assignment paths must share the rule,
    otherwise "blocked" only means "blocked if you happen to use task_update".
    """
    where, args = _scope_where(scope)
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        if not await _deps_complete(conn, scope, task_id):
            team_event("task_bind_blocked", task_id=task_id, actor=owner_agent_id,
                       team_id=scope.team_id, reason="blocked_by_incomplete")
            return {"ok": False, "reason": "blocked"}
        cur = await conn.execute(
            f"UPDATE team_tasks SET status='in_progress',owner_agent_id=?,version=version+1,updated_at=? "
            f"WHERE task_id=? AND {where} AND status='pending' AND owner_agent_id='' AND version=?",
            (owner_agent_id, now_ms(), task_id, *args, int(expected_version)),
        )
        await conn.commit()
        ok = (cur.rowcount or 0) == 1
    if not ok:
        task = await get_task(scope, task_id)
        return {"ok": False, "reason": "not_found" if task is None else "conflict"}
    task = await get_task(scope, task_id)
    assert task is not None
    await emit_event(scope, "task_updated", {"task": task, "action": "spawn_bind"}, task_id=task_id)
    return {"ok": True, "task": task}


# ---------------- messages ----------------
async def send_message(scope: TeamScope, *, recipient: str, kind: str,
                       content: str) -> dict[str, Any]:
    """Enqueue one message row.

    ``recipient`` is normalized to an internal agent id here rather than at the
    call site: every writer (lead tools, teammate tools, retries) must land in
    the same inbox, and a bare name and ``sub:``-prefixed name are the same
    agent. Leaving that to callers is exactly how a message gets stored under
    ``poet`` while the worker polls ``sub:poet`` and never sees it.
    """
    mid = new_id("tmsg")
    ts = now_ms()
    target = normalize_agent_id(recipient) or str(recipient or "").strip()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO team_messages(message_id,team_id,user_id,session_id,generation_id,"
            "sender_agent_id,recipient_agent_id,kind,content,created_at,state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'queued')",
            (mid, scope.team_id, scope.user_id, scope.session_id, scope.generation_id,
             scope.agent_id, target, kind or "note", content, ts),
        )
        await conn.commit()
    msg = {"message_id": mid, "team_id": scope.team_id,
           "sender_agent_id": scope.agent_id, "recipient_agent_id": target,
           "kind": kind or "note", "content": content, "created_at": ts,
           "delivered_at": 0, "read_at": 0, "state": "queued"}
    team_event("message_queued", message_id=mid, sender=scope.agent_id,
               recipient=target, team_id=scope.team_id, kind=kind or "note",
               content_chars=len(content or ""))
    await emit_event(scope, "team_message", {"message": msg})
    return msg


async def list_messages(scope: TeamScope, *, recipient: str = "",
                        unread_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    where, args = _scope_where(scope)
    sql = f"SELECT * FROM team_messages WHERE {where}"
    vals: list[Any] = list(args)
    if recipient:
        sql += " AND recipient_agent_id=?"
        vals.append(recipient)
    if unread_only:
        sql += " AND read_at=0"
    sql += " ORDER BY created_at ASC LIMIT ?"
    vals.append(max(1, min(int(limit), 500)))
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, vals)
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_message_read(scope: TeamScope, message_id: str) -> bool:
    where, args = _scope_where(scope)
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            f"UPDATE team_messages SET read_at=?, state='consumed' "
            f"WHERE message_id=? AND {where} AND read_at=0",
            (now_ms(), message_id, *args),
        )
        await conn.commit()
        return (cur.rowcount or 0) == 1


async def consume_inbox(scope: TeamScope, *, limit: int = 20) -> list[dict[str, Any]]:
    """Take the messages addressed to this agent and mark them consumed.

    This is the delivery step, not a read: the caller is about to inject the
    returned rows into the agent's next model turn. Marking them in the same
    transaction that returns them is what makes delivery at-most-once instead
    of "read forever, inject sometimes".

    Only rows still in ``queued``/``delivered`` are eligible; a crashed consumer
    therefore re-receives nothing it already acknowledged, which is the same
    trade-off CC319 makes (``Sze`` prunes consumed rows).
    """
    target = normalize_agent_id(scope.agent_id) or scope.agent_id
    where, args = _scope_where(scope)
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute(
                f"SELECT * FROM team_messages WHERE {where} "
                "AND recipient_agent_id=? AND state IN ('queued','delivered') "
                "ORDER BY created_at ASC LIMIT ?",
                (*args, target, max(1, min(int(limit), 200))),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            if rows:
                ids = [r["message_id"] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                await conn.execute(
                    f"UPDATE team_messages SET read_at=?, delivered_at=CASE "
                    f"WHEN delivered_at=0 THEN ? ELSE delivered_at END, state='consumed' "
                    f"WHERE message_id IN ({placeholders}) AND {where}",
                    (ts, ts, *ids, *args),
                )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
    for row in rows:
        row["read_at"] = ts
        row["delivered_at"] = row.get("delivered_at") or ts
        row["state"] = "consumed"
    if rows:
        team_event("inbox_consumed", agent=scope.agent_id, team_id=scope.team_id,
                   count=len(rows),
                   message_ids=[r["message_id"] for r in rows[:10]])
    return rows


async def mark_messages_delivered(scope: TeamScope, message_ids: list[str]) -> int:
    """把「已推给活着的 agent」的消息直接标成 consumed。

    这里用的是 consumed 而不是 delivered：这条消息已经进了那个 agent 自己的
    待注入队列，下一次模型调用前必然被看到，它剩下的生命周期只有「被注入」。

    只标 delivered 会留下一个真实缺陷：``consume_inbox`` 把 ``queued`` 和
    ``delivered`` 都当成可投递，于是下一轮补投（``_refill_inbox`` /
    ``deliver_pending_inbox``）会把同一条消息再取一次 —— 队友被安排同一件事
    两遍，lead 的收件箱也会重复注入。真机 steer 场景里 state 停在 delivered
    就是这个问题的痕迹。
    """
    ids = [str(x) for x in (message_ids or []) if str(x)]
    if not ids:
        return 0
    where, args = _scope_where(scope)
    placeholders = ",".join("?" for _ in ids)
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            f"UPDATE team_messages SET read_at=?, delivered_at=?, state='consumed' "
            f"WHERE message_id IN ({placeholders}) AND {where} "
            "AND state IN ('queued','delivered')",
            (ts, ts, *ids, *args),
        )
        await conn.commit()
        return cur.rowcount or 0


# ---------------- name pins ----------------
async def pin_name(scope: TeamScope, name: str, agent_id: str) -> dict[str, Any]:
    """Bind ``name`` to ``agent_id`` on first use; refuse a later rebind.

    Mirrors CC319 ``sendMessagePins`` / GA ``NamePins``. Without this, closing a
    teammate and spawning another one under the same name makes old messages
    silently land on the new agent — the classic cross-talk bug.
    """
    key = (name or "").strip().lower()
    agent = normalize_agent_id(agent_id) or str(agent_id or "").strip()
    if not key or not agent:
        raise ValueError("name and agent_id are required")
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute(
                "SELECT agent_id FROM team_name_pins WHERE team_id=? AND name=?",
                (scope.team_id, key),
            )
            row = await cur.fetchone()
            if row is not None:
                pinned = str(row["agent_id"])
                await conn.rollback()
                if pinned != agent:
                    raise ValueError(
                        f"名字 {key!r} 已钉在 {pinned} 上，不能再绑定到 {agent}；"
                        "请换一个名字或先 close_agent 释放")
                return {"name": key, "agent_id": pinned, "created": False}
            await conn.execute(
                "INSERT INTO team_name_pins(team_id,name,agent_id,pinned_at) VALUES(?,?,?,?)",
                (scope.team_id, key, agent, now_ms()),
            )
            await conn.commit()
        except ValueError:
            raise
        except BaseException:
            await conn.rollback()
            raise
    return {"name": key, "agent_id": agent, "created": True}


async def resolve_name(scope: TeamScope, name: str) -> str:
    """Name -> agent id, honoring an existing pin.

    Unpinned names default to ``sub:<name>`` so a first message to a teammate
    that has not been pinned yet still lands in the right inbox.
    """
    key = (name or "").strip().lower()
    if not key:
        return ""
    if key in LEAD_ALIASES:
        return LEAD_AGENT_ID
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT agent_id FROM team_name_pins WHERE team_id=? AND name=?",
            (scope.team_id, key),
        )
        row = await cur.fetchone()
    if row is not None:
        return str(row[0])
    return normalize_agent_id(name)


async def converge_orphaned_active_teams(user_id: str, session_id: str) -> int:
    """Close active teams that no longer have an active generation lease.

    This is the last line of defence for the TeamPanel: a crash between the
    generation update and the team update could otherwise leave a run marked
    active forever.  It only touches the caller's session and only when there
    is no active lease at all.
    """
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        cur = await conn.execute(
            "UPDATE team_runs SET status='stale',updated_at=?,closed_at=? "
            "WHERE user_id=? AND session_id=? AND status='active' "
            "AND generation_id NOT IN ("
            "  SELECT generation_id FROM session_generations "
            "  WHERE user_id=? AND session_id=? AND status='active'"
            ")",
            (ts, ts, user_id, session_id, user_id, session_id),
        )
        await conn.commit()
        return cur.rowcount or 0


async def recover_stale_runtime(reason: str = "server restart") -> dict[str, int]:
    """Converge leases left behind by a previous server process.

    A single-process web deployment cannot still own an in-memory stop token or
    asyncio task after restart, so keeping these rows active would permanently
    block or misreport the next generation.  Tasks are returned to pending with
    an incremented version, invalidating stale CAS holders.
    """
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute("BEGIN IMMEDIATE")
        try:
            gen_cur = await conn.execute(
                "UPDATE session_generations SET status='stale',finished_at=? "
                "WHERE status='active'", (ts,)
            )
            task_cur = await conn.execute(
                "UPDATE team_tasks SET status='pending',owner_agent_id='',version=version+1,updated_at=? "
                "WHERE status='in_progress'", (ts,)
            )
            team_cur = await conn.execute(
                "UPDATE team_runs SET status='stale',updated_at=?,closed_at=? "
                "WHERE status='active'", (ts, ts)
            )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
    return {
        "generations": gen_cur.rowcount or 0,
        "tasks": task_cur.rowcount or 0,
        "teams": team_cur.rowcount or 0,
    }


async def delete_session_data(user_id: str, session_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        for table in ("team_events", "team_messages", "team_task_deps",
                      "team_tasks", "team_runs", "session_generations"):
            await conn.execute(
                f"DELETE FROM {table} WHERE user_id=? AND session_id=?", (user_id, session_id)
            )
        await conn.commit()
