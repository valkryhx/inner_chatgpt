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


def test_context_usage_reports_compaction_pending(isolated_db):
    """后台赛后压缩在飞时，/context 必须如实说 `compaction_pending: true`。

    这是页脚能自愈的前提：前端看到 true 才有理由稍后重刷。若这里恒 false，
    页脚会一直停在压缩前的偏高读数上（2026-11-15 真机实测到 110.2% 卡在界面上）。

    这里用替身 task 直接钉端点契约（`is_pending` 只读 `_pending` 里 task 的
    `done()`），真正的调度/完成/取消生命周期由 `test_deferred_compaction.py`
    的 `test_is_pending_*` 覆盖 —— 那里才有真实 asyncio 环境。
    """
    from app import deferred_compact

    session = run(db.create_session("u1", "mine"))
    run(history_store.get_history_store().set("u1", session["id"], [
        {"role": "user", "content": "hi"},
    ]))
    key = ("u1", session["id"])
    deferred_compact._pending.clear()

    class _FakeTask:
        def __init__(self, done: bool) -> None:
            self._done = done

        def done(self) -> bool:
            return self._done

    try:
        with make_client("u1") as client:
            base = client.get(f"/api/sessions/{session['id']}/context").json()
            assert base["compaction_pending"] is False, "没有后台任务时必须为 False"

            deferred_compact._pending[key] = _FakeTask(done=False)
            flying = client.get(f"/api/sessions/{session['id']}/context").json()
            assert flying["compaction_pending"] is True, "压缩在飞时必须报 True"

            deferred_compact._pending[key] = _FakeTask(done=True)
            settled = client.get(f"/api/sessions/{session['id']}/context").json()
            assert settled["compaction_pending"] is False, (
                "任务已结束还报 True 会让前端一直轮询"
            )
    finally:
        deferred_compact._pending.clear()
