"""Context usage endpoint: owner isolation and same token accounting as compaction."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, history_store
from app.auth import get_current_user
from app.history_store import HistoryStore
from app.routers import sessions as sessions_router
from app.tokens import message_token_counter


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(history_store, "DB_PATH", path)
    run(db.init_db())
    store = HistoryStore()
    run(store.init())
    history_store.set_history_store(store)
    yield path
    history_store.set_history_store(None)


def make_client(uid: str) -> TestClient:
    app = FastAPI()
    app.include_router(sessions_router.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": uid}
    return TestClient(app)


def test_context_usage_uses_model_limits_and_history_tokens(isolated_db):
    session = run(db.create_session("u1", "mine"))
    run(db.create_llm_config(
        "u1", label="test", base_url="http://example.invalid", api_key="x",
        model="demo", context_window=100000, auto_compact_tokens=80000,
        hard_limit_tokens=95000, is_default=True,
    ))
    run(db.update_session(session["id"], "u1", model_id=(
        run(db.list_llm_configs("u1"))[0]["id"]
    )))
    messages = [
        {"role": "user", "content": "你好，请帮我计算。"},
        {"role": "assistant", "content": "好的，我来处理。"},
    ]
    run(history_store.get_history_store().set("u1", session["id"], messages))
    expected = message_token_counter(messages)

    with make_client("u1") as client:
        body = client.get(f"/api/sessions/{session['id']}/context").json()

    assert body["used_tokens"] == expected
    assert body["context_window"] == 100000
    assert body["auto_compact_tokens"] == 80000
    assert body["hard_limit_tokens"] == 95000
    assert body["percent"] == round(expected / 100000 * 100, 2)
    assert body["remaining_to_compact"] == 80000 - expected
    assert body["compacted"] is False


def test_context_usage_does_not_leak_across_users(isolated_db):
    session = run(db.create_session("u1", "mine"))
    with make_client("u2") as client:
        assert client.get(f"/api/sessions/{session['id']}/context").status_code == 404
