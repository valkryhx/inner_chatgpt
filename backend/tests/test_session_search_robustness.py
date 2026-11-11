"""搜索功能的健壮性单测：畸形响应、边界输入、用户隔离。

前两组对应曾经真实发生过的白屏事故：
  - hits 不是数组       → Cannot read properties of null (reading 'length')
  - hits 里含 null 元素 → Cannot read properties of null (reading 'role')
前端已加 normalizeResult 兜底 + ErrorBoundary，这里在 API 层锁死契约。
"""
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
    sess = run(db.create_session(uid, title))
    for role, content in texts:
        run(db.add_message(sess["id"], role, content=content))
    return sess


# ---------------- 边界与恶意输入：不得抛异常 ----------------

NASTY_QUERIES = [
    ("空串", ""),
    ("纯空白", "   "),
    ("单个字符", "的"),
    ("超长 5000 字符", "x" * 5000),
    ("百分号", "%"),
    ("下划线", "_"),
    ("反斜杠", "\\"),
    ("反斜杠加百分号", "\\%"),
    ("单引号", "'"),
    ("SQL 注入", "'; DROP TABLE messages; --"),
    ("UNION 注入", "' UNION SELECT * FROM users --"),
    ("换行", "\n"),
    ("NULL 字符", "\x00"),
    ("emoji", "🚀🔥"),
    ("正则元字符", ".*+?^${}()|[]"),
    ("零宽字符", "​‌"),
    ("超长换行", "\n" * 500),
]


@pytest.mark.parametrize("label,q", NASTY_QUERIES)
def test_nasty_query_never_raises(isolated_db, label, q):
    """任何输入都只能返回结果或空，不能抛异常——异常会冒到 HTTP 层变 500。"""
    seed("u1", "靶子", [("user", "一些正常内容 the needle")])
    with make_client("u1") as client:
        resp = client.get("/api/sessions/search", params={"q": q})
    assert resp.status_code == 200, f"{label} 返回 {resp.status_code}"
    body = resp.json()
    assert isinstance(body["hits"], list)


def test_sql_injection_does_not_destroy_data(isolated_db):
    seed("u1", "靶子", [("user", "重要数据不能丢")])
    with make_client("u1") as client:
        client.get("/api/sessions/search", params={"q": "'; DROP TABLE messages; --"})
        # 表还在、数据还在
        assert run(db.count_messages(seed("u1", "另一个", [])["id"])) == 0
        hits = client.get("/api/sessions/search", params={"q": "重要数据"}).json()["hits"]
    assert len(hits) == 1


# ---------------- 片段不变量：前端高亮依赖这些 ----------------

@pytest.mark.parametrize("content,q", [
    ("很短", "很"),
    ("前" * 500 + "针" + "后" * 500, "针"),
    ("\n".join(f"第{i}行" for i in range(300)), "第200行"),
    ("emoji 🚀 前后", "🚀"),
    ("只在开头 needle" + "填充" * 300, "needle"),
    ("填充" * 300 + "只在结尾needle", "needle"),
])
def test_snippet_invariants(isolated_db, content, q):
    """offset/length 必须落在片段内，否则前端 slice 会错位或高亮错文字。"""
    seed("u1", "片段", [("user", content)])
    with make_client("u1") as client:
        hits = client.get("/api/sessions/search", params={"q": q}).json()["hits"]

    assert len(hits) == 1
    h = hits[0]
    snippet = h["snippet"]
    off, ln = h["match_offset"], h["match_length"]
    assert isinstance(snippet, str)
    assert ln > 0
    if off >= 0:
        assert off + ln <= len(snippet), f"越界: {off}+{ln} > {len(snippet)}"
        # 高亮段必须真的含目标字符（大小写不敏感）
        assert snippet[off:off + ln].lower() == q.lower()
    # 片段不能把整段超长正文塞回来
    assert len(snippet) <= 400, f"片段 {len(snippet)} 字符过长"


def test_long_content_snippet_is_bounded(isolated_db):
    seed("u1", "超长", [("user", "前" * 50000 + "靶心" + "后" * 50000)])
    with make_client("u1") as client:
        hits = client.get("/api/sessions/search", params={"q": "靶心"}).json()["hits"]
    assert len(hits[0]["snippet"]) < 400


# ---------------- 隔离不因脏输入失效 ----------------

@pytest.mark.parametrize("q", ["任务", "%", "_", "\\", "'", "needle"])
def test_isolation_holds_for_nasty_queries(isolated_db, q):
    seed("u1", "u1 的", [("user", f"u1 内容 {q} needle 任务")])
    seed("u2", "u2 的", [("user", f"u2 内容 {q} needle 任务")])
    with make_client("u1") as client:
        hits = client.get("/api/sessions/search", params={"q": q}).json()["hits"]
    # 只应有自己会话的命中
    for h in hits:
        assert h["session_title"] == "u1 的", f"泄漏了 {h['session_title']}"


# ---------------- limit / 分页边界 ----------------

@pytest.mark.parametrize("limit", [0, -5, 1, 100, 10000])
def test_limit_is_clamped_and_safe(isolated_db, limit):
    seed("u1", "多", [("user", f"命中 hit 第{i}条") for i in range(5)])
    with make_client("u1") as client:
        resp = client.get("/api/sessions/search", params={"q": "hit", "limit": limit})
    assert resp.status_code == 200
    hits = resp.json()["hits"]
    # 路由层夹紧到 [1, 100]，任何输入都不能返回超过 100 条或负数
    assert 0 <= len(hits) <= 100


def test_has_more_consistency(isolated_db):
    seed("u1", "多", [("user", f"命中 hit {i}") for i in range(10)])
    with make_client("u1") as client:
        few = client.get("/api/sessions/search", params={"q": "hit", "limit": 3}).json()
        many = client.get("/api/sessions/search", params={"q": "hit", "limit": 100}).json()
    assert len(few["hits"]) == 3 and few["has_more"] is True
    assert len(many["hits"]) == 10 and many["has_more"] is False


# ---------------- 响应契约：前端依赖的字段必须存在且类型正确 ----------------

def test_response_shape_matches_contract(isolated_db):
    seed("u1", "契约", [("user", "契约关键词 CONTRACT")])
    with make_client("u1") as client:
        body = client.get("/api/sessions/search", params={"q": "CONTRACT"}).json()

    assert set(body) >= {"query", "hits", "has_more", "elapsed_ms"}
    assert isinstance(body["hits"], list)
    assert isinstance(body["has_more"], bool)
    assert isinstance(body["elapsed_ms"], (int, float))

    h = body["hits"][0]
    assert isinstance(h["message_id"], str) and h["message_id"]
    assert isinstance(h["session_id"], str) and h["session_id"]
    assert isinstance(h["session_title"], str)
    assert h["role"] in ("user", "assistant")   # 绝不返回 system
    assert isinstance(h["snippet"], str)
    assert isinstance(h["match_offset"], int)
    assert isinstance(h["match_length"], int)
    assert isinstance(h["created_at"], int)


def test_never_returns_system_messages(isolated_db):
    sess = run(db.create_session("u1", "含 system"))
    run(db.add_message(sess["id"], "system", content="系统注入 SECRETSYSTEM"))
    run(db.add_message(sess["id"], "user", content="用户 SECRETSYSTEM 提问"))
    with make_client("u1") as client:
        hits = client.get("/api/sessions/search", params={"q": "SECRETSYSTEM"}).json()["hits"]
    assert all(h["role"] != "system" for h in hits)
    assert len(hits) == 1


def test_empty_query_response_is_valid(isolated_db):
    """空查询也必须返回合法结构，前端 normalize 前不能崩。"""
    with make_client("u1") as client:
        body = client.get("/api/sessions/search", params={"q": ""}).json()
    assert body["hits"] == []
    assert body["has_more"] is False