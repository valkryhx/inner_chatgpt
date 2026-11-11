"""搜索端点：用户隔离、关键词转义、片段位置、空查询与分页。"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, history_store
from app.auth import get_current_user
from app.history_store import HistoryStore
from app.routers import sessions as sessions_router


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


def seed(uid: str, title: str, texts: list[tuple[str, str]]) -> dict:
    """建一个会话并按顺序塞入 (role, content) 消息。"""
    sess = run(db.create_session(uid, title))
    for role, content in texts:
        run(db.add_message(sess["id"], role, content=content))
    return sess


def test_search_finds_body_text_and_returns_session(isolated_db):
    sess = seed("u1", "关于任务板的讨论", [
        ("user", "帮我看看 Agent Team 的任务板怎么用"),
        ("assistant", "任务板是持久化的，lead 建任务后 teammate 认领。"),
    ])
    with make_client("u1") as client:
        body = client.get("/api/sessions/search", params={"q": "任务板"}).json()

    assert body["query"] == "任务板"
    assert len(body["hits"]) == 2
    first = body["hits"][0]
    assert first["session_id"] == sess["id"]
    assert first["session_title"] == "关于任务板的讨论"
    # 片段里必须真的含关键词，且 offset 指向它（前端据此高亮）。
    assert "任务板" in first["snippet"]
    assert first["snippet"][first["match_offset"]: first["match_offset"] + 3] == "任务板"
    assert first["match_length"] == 3


def test_search_isolates_users(isolated_db):
    seed("u1", "u1 的会话", [("user", "这是 u1 的机密内容 alpha")])
    seed("u2", "u2 的会话", [("user", "这是 u2 的机密内容 beta")])

    with make_client("u1") as client:
        body = client.get("/api/sessions/search", params={"q": "机密"}).json()

    # 只能看到自己的那条：messages 表没有 user_id，泄漏只会发生在漏写 join 时。
    assert len(body["hits"]) == 1
    assert "alpha" in body["hits"][0]["snippet"]
    assert body["hits"][0]["session_title"] == "u1 的会话"


def test_search_skips_system_and_archived(isolated_db):
    live = seed("u1", "活的会话", [("user", "关键词 zebra 在这里")])
    archived = seed("u1", "归档会话", [("user", "关键词 zebra 也在归档里")])
    run(db.update_session(archived["id"], "u1", archived=True))
    # system 消息是平台注入的，用户看不见它，不该出现在搜索结果里。
    run(db.add_message(live["id"], "system", content="关键词 zebra 藏在 system"))

    with make_client("u1") as client:
        body = client.get("/api/sessions/search", params={"q": "zebra"}).json()

    assert len(body["hits"]) == 1
    assert body["hits"][0]["session_id"] == live["id"]


def test_search_escapes_like_wildcards(isolated_db):
    seed("u1", "百分号", [("user", "命中率 50% 的那条")])
    seed("u1", "别的", [("user", "完全无关的一条 500 次")])

    with make_client("u1") as client:
        # 未转义时 "50%" 的 % 是通配符，会把 "500 次" 也命中。
        body = client.get("/api/sessions/search", params={"q": "50%"}).json()

    snippets = [h["snippet"] for h in body["hits"]]
    assert any("50%" in s for s in snippets)
    assert not any("500 次" in s for s in snippets)


def test_search_underscore_is_literal(isolated_db):
    seed("u1", "下划线", [("user", "变量名 a_b 在这个文件里")])
    seed("u1", "干扰", [("user", "变量名 axb 也是相似的")])

    with make_client("u1") as client:
        body = client.get("/api/sessions/search", params={"q": "a_b"}).json()

    assert len(body["hits"]) == 1
    assert "a_b" in body["hits"][0]["snippet"]


def test_search_empty_query_returns_nothing(isolated_db):
    seed("u1", "有内容", [("user", "随便什么内容")])
    with make_client("u1") as client:
        body = client.get("/api/sessions/search", params={"q": "   "}).json()
    assert body["hits"] == []
    assert body["has_more"] is False


def test_search_has_more_and_limit(isolated_db):
    texts = [("user", f"重复关键词 hit 第 {i} 条") for i in range(6)]
    seed("u1", "很多命中", texts)

    with make_client("u1") as client:
        page1 = client.get("/api/sessions/search", params={"q": "hit", "limit": 4}).json()
        page2 = client.get("/api/sessions/search", params={"q": "hit", "limit": 100}).json()

    assert len(page1["hits"]) == 4
    assert page1["has_more"] is True
    assert len(page2["hits"]) == 6
    assert page2["has_more"] is False


def test_search_scoped_to_session(isolated_db):
    a = seed("u1", "会话 A", [("user", "共同词 shared")])
    seed("u1", "会话 B", [("user", "共同词 shared 也在 B")])

    with make_client("u1") as client:
        body = client.get(
            "/api/sessions/search", params={"q": "shared", "session_id": a["id"]}
        ).json()

    assert len(body["hits"]) == 1
    assert body["hits"][0]["session_id"] == a["id"]


def test_search_result_order_newest_first(isolated_db):
    sess = run(db.create_session("u1", "顺序"))
    run(db.add_message(sess["id"], "user", content="关键词 order 第一条"))
    run(db.add_message(sess["id"], "assistant", content="关键词 order 第二条"))

    with make_client("u1") as client:
        body = client.get("/api/sessions/search", params={"q": "order"}).json()

    created = [h["created_at"] for h in body["hits"]]
    assert created == sorted(created, reverse=True)


def test_snippet_marks_truncation(isolated_db):
    long_text = "前" * 200 + "关键词 needle" + "后" * 200
    seed("u1", "长文本", [("user", long_text)])

    with make_client("u1") as client:
        body = client.get("/api/sessions/search", params={"q": "needle"}).json()

    snippet = body["hits"][0]["snippet"]
    assert "needle" in snippet
    # 两侧都被裁掉时必须加省略号，否则用户会把片段当成全文开头。
    assert snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) < len(long_text)


def test_search_requires_auth(isolated_db):
    app = FastAPI()
    app.include_router(sessions_router.router)
    with TestClient(app) as client:
        resp = client.get("/api/sessions/search", params={"q": "x"})
    assert resp.status_code == 401