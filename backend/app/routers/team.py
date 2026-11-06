"""Read-only Agent Team endpoints scoped to an owned session."""
from __future__ import annotations

import json
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from .. import db, team_store
from ..auth import get_current_user
from ..scope import TeamScope
from ..team_host import TeamHost

router = APIRouter(prefix="/api/sessions/{session_id}/team", tags=["team"])


async def _owned_session(session_id: str, user: dict) -> dict[str, Any]:
    session = await db.get_session(session_id, user["id"])
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


async def _latest_team(conn: aiosqlite.Connection, user_id: str,
                       session_id: str) -> dict[str, Any] | None:
    conn.row_factory = aiosqlite.Row
    cur = await conn.execute(
        "SELECT * FROM team_runs WHERE user_id=? AND session_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id, session_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def _team_scope(session_id: str, user: dict) -> tuple[dict[str, Any] | None, TeamScope | None]:
    await _owned_session(session_id, user)
    # Do not report a permanently-active team after its generation is gone.
    # The read endpoint is also a refresh recovery point for the TeamPanel.
    from ..runtime import has_active_generation
    if not has_active_generation(session_id, user["id"]):
        active_generation = await team_store.get_active_generation(user["id"], session_id)
        if active_generation:
            await team_store.converge_generation_facts(
                user["id"], session_id, active_generation, status="stale"
            )
        else:
            await team_store.converge_orphaned_active_teams(user["id"], session_id)
    async with aiosqlite.connect(team_store.DB_PATH) as conn:
        team = await _latest_team(conn, user["id"], session_id)
    if team is None:
        return None, None
    return team, TeamScope(
        user_id=user["id"], session_id=session_id,
        generation_id=team["generation_id"], team_id=team["team_id"],
        agent_id=team.get("lead_agent_id") or "root", role="lead",
    )


def _event_out(event: dict[str, Any]) -> dict[str, Any]:
    return TeamHost.event_shape(event)


@router.get("")
async def get_team(session_id: str, user: dict = Depends(get_current_user)):
    team, scope = await _team_scope(session_id, user)
    if team is None or scope is None:
        return {"team": None, "tasks": [], "events": [], "messages": []}
    host = TeamHost(scope)
    return {
        "team": team,
        "tasks": await host.list_tasks(),
        "events": [_event_out(e) for e in await host.events_since(0, limit=200)],
        "messages": await host.list_messages(limit=100),
    }


@router.get("/tasks")
async def get_tasks(session_id: str, user: dict = Depends(get_current_user)):
    _team, scope = await _team_scope(session_id, user)
    if scope is None:
        return {"tasks": []}
    return {"tasks": await TeamHost(scope).list_tasks()}


@router.get("/events")
async def get_events(session_id: str, since_seq: int = Query(0, ge=0),
                     user: dict = Depends(get_current_user)):
    _team, scope = await _team_scope(session_id, user)
    if scope is None:
        return {"events": [], "since_seq": since_seq}
    return {
        "events": [_event_out(e) for e in await TeamHost(scope).events_since(since_seq, 500)],
        "since_seq": since_seq,
    }


@router.get("/messages")
async def get_messages(session_id: str, recipient: str = "", unread_only: bool = False,
                       limit: int = Query(100, ge=1, le=500),
                       user: dict = Depends(get_current_user)):
    _team, scope = await _team_scope(session_id, user)
    if scope is None:
        return {"messages": []}
    return {"messages": await TeamHost(scope).list_messages(
        recipient=recipient, unread_only=unread_only, limit=limit
    )}
