"""用户级 A2A 开关：默认关、热生效、实例级 env 是硬闸。"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.auth import get_current_user


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    from app.a2a import store

    path = tmp_path / "app.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setenv("INNER_A2A_ENABLED", "true")
    monkeypatch.setenv("INNER_A2A_PUBLIC_BASE_URL", "https://hlaudz.example.test")
    run(db.init_db())
    run(store.init_tables())

    async def seed_user(uid):
        async with aiosqlite.connect(path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO users(id,email,username,password_hash,name,role,status,token_ver,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (uid, f"{uid}@test.invalid", uid, "x", uid, "user", "active", 0, 0),
            )
            await conn.commit()

    run(seed_user("u1"))
    yield path


def _app():
    from app.a2a.router import api_router, router

    app = FastAPI()
    app.include_router(router)
    app.include_router(api_router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}
    return app


def _token(client, name="cli"):
    return client.post("/api/a2a/tokens", json={"name": name}).json()


def test_agent_defaults_to_disabled(isolated_db):
    from app.a2a import store

    agent = run(store.ensure_agent("u1"))
    assert agent["enabled"] is False

    with TestClient(_app()) as client:
        created = _token(client)
        aid = created["agent_id"]
        assert client.get(f"/a2a/{aid}/.well-known/agent-card.json").status_code == 404
        body = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}}
        r = client.post(f"/a2a/{aid}", json=body,
                        headers={"Authorization": f"Bearer {created['token']}"})
        assert r.status_code == 404


def test_toggle_is_hot(isolated_db):
    from app.a2a import store

    with TestClient(_app()) as client:
        created = _token(client)
        aid, raw = created["agent_id"], created["token"]
        hdr = {"Authorization": f"Bearer {raw}"}

        on = client.post("/api/a2a/agent/enabled", json={"enabled": True})
        assert on.status_code == 200
        assert on.json()["enabled"] is True
        assert client.get(f"/a2a/{aid}/.well-known/agent-card.json").status_code == 200

        off = client.post("/api/a2a/agent/enabled", json={"enabled": False})
        assert off.json()["enabled"] is False
        assert client.get(f"/a2a/{aid}/.well-known/agent-card.json").status_code == 404
        assert client.post(f"/a2a/{aid}", json={"jsonrpc": "2.0", "id": 1,
                                                "method": "GetTask",
                                                "params": {"id": "x"}},
                           headers=hdr).status_code == 404


def test_token_survives_toggle_cycle(isolated_db):
    from app.a2a import auth, store

    with TestClient(_app()) as client:
        created = _token(client)
        aid, raw = created["agent_id"], created["token"]
        client.post("/api/a2a/agent/enabled", json={"enabled": True})
        client.post("/api/a2a/agent/enabled", json={"enabled": False})
        client.post("/api/a2a/agent/enabled", json={"enabled": True})
        ident = run(auth.authenticate(f"Bearer {raw}", aid, None))
        assert ident["user_id"] == "u1"


def test_instance_env_is_a_hard_gate(isolated_db, monkeypatch):
    from app.a2a import store

    run(store.ensure_agent("u1"))
    run(store.set_agent_enabled("u1", True))
    monkeypatch.setenv("INNER_A2A_ENABLED", "false")

    with TestClient(_app()) as client:
        assert client.get("/api/a2a/agent").status_code == 404
        agent = run(store.get_agent("u1"))
        assert client.get(
            f"/a2a/{agent['agent_id']}/.well-known/agent-card.json"
        ).status_code == 404


def test_management_api_reports_switch_state(isolated_db):
    from app.a2a import store

    with TestClient(_app()) as client:
        _token(client)
        info = client.get("/api/a2a/agent").json()
        assert info["enabled"] is False
        assert info["instance_enabled"] is True

        client.post("/api/a2a/agent/enabled", json={"enabled": True})
        assert client.get("/api/a2a/agent").json()["enabled"] is True


def test_migration_defaults_existing_agents_to_disabled(tmp_path, monkeypatch):
    """老库（没有 enabled 列）升级后一律是关，不会因为升级自动对外可达。"""
    from app.a2a import store

    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", path)

    async def seed_old():
        async with aiosqlite.connect(path) as conn:
            await conn.execute(
                "CREATE TABLE a2a_agents (id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE,"
                " agent_id TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL)"
            )
            await conn.execute(
                "INSERT INTO a2a_agents VALUES('a2a_1','u1','ha_old',123)"
            )
            await conn.commit()

    run(seed_old())
    run(store.init_tables())
    agent = run(store.get_agent("u1"))
    assert agent["agent_id"] == "ha_old"
    assert agent["enabled"] is False
