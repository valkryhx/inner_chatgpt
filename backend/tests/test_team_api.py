"""Team HTTP API isolation and empty-team behavior."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, team_store
from app.auth import get_current_user
from app.routers import team as team_router
from app.scope import TeamScope
from app.team_host import TeamHost


def run(coro):
    return asyncio.run(coro)


class FakeQuota:
    def release(self):
        pass

    async def team_lease(self, sc, *, timeout_s=0.0):
        return self

    @asynccontextmanager
    async def team(self, sc, *, timeout_s=0.0):
        yield await self.team_lease(sc, timeout_s=timeout_s)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(team_store, "DB_PATH", path)
    run(db.init_db())
    run(team_store.init_tables())
    return path


def make_client(uid: str) -> TestClient:
    app = FastAPI()
    app.include_router(team_router.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": uid}
    return TestClient(app)


def seed(uid="u1", sid="s1", tid="t1", gid="g1"):
    async def flow():
        s = TeamScope(uid, sid, gid, tid, "root", "lead")
        host = TeamHost(s, quota_manager=FakeQuota())
        await host.create("demo")
        task = await host.create_task(subject="task")
        await host.send_message(recipient="sub:a", content="hello")
        return task
    return run(flow())


def test_missing_session_is_404(isolated_db):
    with make_client("u1") as c:
        assert c.get("/api/sessions/nope/team").status_code == 404


def test_other_users_session_is_404(isolated_db):
    run(db.create_session("u1", "mine"))
    with make_client("u2") as c:
        assert c.get("/api/sessions/co_x/team").status_code == 404


def test_empty_team_returns_null_and_empty_lists(isolated_db):
    session = run(db.create_session("u1", "mine"))
    with make_client("u1") as c:
        body = c.get(f"/api/sessions/{session['id']}/team").json()
        assert body == {"team": None, "tasks": [], "events": [], "messages": []}
        assert c.get(f"/api/sessions/{session['id']}/team/tasks").json() == {"tasks": []}
        assert c.get(f"/api/sessions/{session['id']}/team/messages").json() == {"messages": []}
        assert c.get(f"/api/sessions/{session['id']}/team/events", params={"since_seq": 0}).json() == {
            "events": [], "since_seq": 0,
        }


def test_normal_team_tasks_events_messages(isolated_db):
    session = run(db.create_session("u1", "mine"))
    seed(sid=session["id"], tid="t1")
    with make_client("u1") as c:
        body = c.get(f"/api/sessions/{session['id']}/team").json()
        assert body["team"]["team_id"] == "t1"
        assert any(t["subject"] == "task" for t in body["tasks"])
        assert body["events"][0]["type"] == "team_start"
        assert body["messages"][0]["content"] == "hello"
        tasks = c.get(f"/api/sessions/{session['id']}/team/tasks").json()["tasks"]
        assert tasks[0]["user_id"] == "u1"
        events = c.get(f"/api/sessions/{session['id']}/team/events",
                       params={"since_seq": 1}).json()["events"]
        assert all(e["event_seq"] > 1 for e in events)
        msgs = c.get(f"/api/sessions/{session['id']}/team/messages",
                     params={"recipient": "sub:a"}).json()["messages"]
        assert len(msgs) == 1


def test_api_does_not_leak_across_users_or_sessions(isolated_db):
    s1 = run(db.create_session("u1", "one"))
    s2 = run(db.create_session("u1", "two"))
    seed(uid="u1", sid=s1["id"], tid="t1")
    with make_client("u1") as c:
        assert c.get(f"/api/sessions/{s2['id']}/team").json()["team"] is None
    with make_client("u2") as c:
        assert c.get(f"/api/sessions/{s1['id']}/team").status_code == 404
