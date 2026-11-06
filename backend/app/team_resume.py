"""Copy an unfinished Team board across generation boundaries.

The Team runtime is intentionally generation-scoped.  A user message can still
continue a board that outlived the previous generation, so we snapshot the
latest unfinished board into the new generation instead of reusing mutable rows.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from .scope import TeamScope
from .team_store import TASK_STATUSES, new_id


async def copy_resume_taskboard(
    conn: aiosqlite.Connection, scope: TeamScope, ts: int
) -> dict[str, Any] | None:
    conn.row_factory = aiosqlite.Row
    cur = await conn.execute(
        "SELECT r.team_id,r.generation_id FROM team_runs r "
        "WHERE r.user_id=? AND r.session_id=? AND r.team_id<>? "
        "AND r.status IN ('completed','stopped','failed','stale') "
        "AND EXISTS ("
        "  SELECT 1 FROM team_tasks t WHERE t.user_id=r.user_id "
        "  AND t.session_id=r.session_id AND t.generation_id=r.generation_id "
        "  AND t.team_id=r.team_id AND t.status IN ('pending','in_progress')"
        ") ORDER BY r.updated_at DESC,r.created_at DESC LIMIT 1",
        (scope.user_id, scope.session_id, scope.team_id),
    )
    source = await cur.fetchone()
    if source is None:
        return None
    source_team_id, source_generation_id = str(source[0]), str(source[1])

    cur = await conn.execute(
        "SELECT * FROM team_tasks WHERE user_id=? AND session_id=? "
        "AND generation_id=? AND team_id=? ORDER BY created_at ASC",
        (scope.user_id, scope.session_id, source_generation_id, source_team_id),
    )
    source_tasks = [dict(row) for row in await cur.fetchall()]
    if not source_tasks or not any(
        t.get("status") in ("pending", "in_progress") for t in source_tasks
    ):
        return None

    task_map: dict[str, str] = {}
    deps_by_task: dict[str, list[str]] = {}
    copied_tasks: list[dict[str, Any]] = []
    for index, old in enumerate(source_tasks):
        new_task_id = new_id("task")
        task_map[str(old["task_id"])] = new_task_id
        status = str(old.get("status") or "pending")
        if status == "in_progress":
            status = "pending"
        if status not in TASK_STATUSES:
            status = "pending"
        owner = str(old.get("owner_agent_id") or "")
        if status != "completed":
            owner = ""
        created_at = ts + index
        subject = str(old.get("subject") or "")
        description = str(old.get("description") or "")
        created_by = str(old.get("created_by") or scope.agent_id)
        await conn.execute(
            "INSERT INTO team_tasks(task_id,team_id,user_id,session_id,generation_id,subject,description,"
            "status,owner_agent_id,created_by,created_at,updated_at,version) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (new_task_id, scope.team_id, scope.user_id, scope.session_id, scope.generation_id,
             subject, description, status, owner, created_by, created_at, created_at),
        )
        copied_tasks.append({
            "task_id": new_task_id,
            "team_id": scope.team_id,
            "user_id": scope.user_id,
            "session_id": scope.session_id,
            "generation_id": scope.generation_id,
            "subject": subject,
            "description": description,
            "status": status,
            "owner_agent_id": owner,
            "created_by": created_by,
            "created_at": created_at,
            "updated_at": created_at,
            "version": 0,
            "blockedBy": [],
        })
    source_ids = list(task_map.keys())
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        cur = await conn.execute(
            "SELECT task_id,blocked_by FROM team_task_deps WHERE user_id=? AND session_id=? "
            f"AND generation_id=? AND team_id=? AND task_id IN ({placeholders}) "
            "ORDER BY task_id,blocked_by",
            (scope.user_id, scope.session_id, source_generation_id, source_team_id, *source_ids),
        )
        for dep_row in await cur.fetchall():
            old_task_id, old_dep_id = str(dep_row[0]), str(dep_row[1])
            new_task_id = task_map.get(old_task_id)
            new_dep_id = task_map.get(old_dep_id)
            if not new_task_id or not new_dep_id:
                continue
            await conn.execute(
                "INSERT OR IGNORE INTO team_task_deps(user_id,session_id,generation_id,team_id,task_id,blocked_by) "
                "VALUES(?,?,?,?,?,?)",
                (scope.user_id, scope.session_id, scope.generation_id, scope.team_id,
                 new_task_id, new_dep_id),
            )
            deps_by_task.setdefault(new_task_id, []).append(new_dep_id)
    for task in copied_tasks:
        task["blockedBy"] = sorted(deps_by_task.get(task["task_id"], []))

    cur = await conn.execute(
        "SELECT * FROM team_messages WHERE user_id=? AND session_id=? "
        "AND generation_id=? AND team_id=? ORDER BY created_at DESC LIMIT 200",
        (scope.user_id, scope.session_id, source_generation_id, source_team_id),
    )
    source_messages = list(reversed(await cur.fetchall()))
    copied_messages: list[dict[str, Any]] = []
    for index, old in enumerate(source_messages):
        row = dict(old)
        new_message_id = new_id("tmsg")
        created_at = ts + len(copied_tasks) + index
        sender = str(row.get("sender_agent_id") or scope.agent_id)
        recipient = str(row.get("recipient_agent_id") or "lead")
        kind = str(row.get("kind") or "note")
        content = str(row.get("content") or "")
        await conn.execute(
            "INSERT INTO team_messages(message_id,team_id,user_id,session_id,generation_id,"
            "sender_agent_id,recipient_agent_id,kind,content,created_at,delivered_at,read_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_message_id, scope.team_id, scope.user_id, scope.session_id, scope.generation_id,
             sender, recipient, kind, content, created_at, 0, 0),
        )
        copied_messages.append({
            "message_id": new_message_id,
            "team_id": scope.team_id,
            "user_id": scope.user_id,
            "session_id": scope.session_id,
            "generation_id": scope.generation_id,
            "sender_agent_id": sender,
            "recipient_agent_id": recipient,
            "kind": kind,
            "content": content,
            "created_at": created_at,
            "delivered_at": 0,
            "read_at": 0,
        })

    return {
        "source_team_id": source_team_id,
        "source_generation_id": source_generation_id,
        "task_count": len(copied_tasks),
        "open_task_count": sum(
            1 for t in copied_tasks if t["status"] in ("pending", "in_progress")
        ),
        "message_count": len(copied_messages),
        "tasks": copied_tasks,
        "messages": copied_messages,
    }
