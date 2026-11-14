"""记忆 REST 路由（设置页「我的记忆」tab 的后端）。

覆盖：列举分页、读、写、删、跨用户隔离、CAS 冲突、字段「未给 vs 清空」语义。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import memory_store as ms
from app.auth import get_current_user
from app.routers import memory as memory_router


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "MEMORIES_DIR", tmp_path / "memories")
    monkeypatch.setattr(ms, "DB_PATH", tmp_path / "app.db")
    run(ms.init_tables())

    app = FastAPI()
    app.include_router(memory_router.router)
    current = {"id": "u1"}
    app.dependency_overrides[get_current_user] = lambda: current

    c = TestClient(app)
    c.current = current  # type: ignore[attr-defined]
    return c


def _create(c, **body):
    defaults = {"slug": "prefs", "title": "称呼", "description": "叫老徐",
                "content": "# 称呼\n\n叫老徐。\n", "kind": "user"}
    defaults.update(body)
    return c.post("/api/me/memories", json=defaults)


# ---------------- 基本 CRUD ----------------
def test_create_list_get_delete(client):
    r = _create(client)
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "prefs"

    listed = client.get("/api/me/memories").json()
    assert listed["total"] == 1
    assert listed["entries"][0]["slug"] == "prefs"
    assert "content" not in listed["entries"][0]

    detail = client.get("/api/me/memories/prefs").json()
    assert "老徐" in detail["content"]

    assert client.delete("/api/me/memories/prefs").status_code == 200
    assert client.get("/api/me/memories").json()["total"] == 0
    assert client.get("/api/me/memories/prefs").status_code == 404


def test_slug_derived_from_title_when_omitted(client):
    r = _create(client, slug="", title="User Name Pref", description="x")
    assert r.status_code == 200
    assert r.json()["slug"] == "user-name-pref"


def test_slug_with_no_ascii_falls_back_to_generated(client):
    r = _create(client, slug="", title="中文标题", description="x")
    assert r.status_code == 200
    assert r.json()["slug"].startswith("memory-")


def test_status_endpoint(client):
    _create(client)
    st = client.get("/api/me/memories/status").json()
    assert st["enabled"] is True
    assert st["auto_write"] is False, "自动提取必须默认关闭"
    assert st["total"] == 1


def test_status_reflects_env(client, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_AUTO_WRITE", "1")
    st = client.get("/api/me/memories/status").json()
    assert st["auto_write"] is True


# ---------------- 隔离 ----------------
def test_cross_user_isolation(client):
    _create(client)
    client.current["id"] = "u2"  # type: ignore[attr-defined]
    assert client.get("/api/me/memories").json()["total"] == 0
    assert client.get("/api/me/memories/prefs").status_code == 404
    # 删不到别人的
    assert client.delete("/api/me/memories/prefs").status_code == 404
    client.current["id"] = "u1"  # type: ignore[attr-defined]
    assert client.get("/api/me/memories/prefs").status_code == 200


# ---------------- 分页 ----------------
def test_pagination_via_api(client):
    for i in range(55):
        _create(client, slug=f"m{i:03d}", description=f"第 {i} 条")
    page1 = client.get("/api/me/memories?limit=50").json()
    assert len(page1["entries"]) == 50 and page1["remaining"] == 5
    page2 = client.get(f"/api/me/memories?limit=50&cursor={page1['next_cursor']}").json()
    assert len(page2["entries"]) == 5 and page2["remaining"] == 0


def test_page_sorted_by_updated_desc(client):
    """存储层按 slug 排序（cursor 要稳定），接口页内按更新时间倒序（设置页直觉）。"""
    _create(client, slug="aaa", description="先写")
    _create(client, slug="zzz", description="后写")
    entries = client.get("/api/me/memories").json()["entries"]
    assert entries[0]["slug"] == "zzz"


# ---------------- PATCH 语义 ----------------
def test_patch_partial_keeps_other_fields(client):
    _create(client, tags=["称呼"])
    r = client.patch("/api/me/memories/prefs", json={"content": "改过的正文\n"})
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "叫老徐"
    assert r.json()["tags"] == ["称呼"]
    assert "改过的正文" in client.get("/api/me/memories/prefs").json()["content"]


def test_patch_can_clear_field_explicitly(client):
    _create(client)
    r = client.patch("/api/me/memories/prefs", json={"description": ""})
    assert r.status_code == 200
    assert r.json()["description"] == ""


def test_patch_missing_slug_404(client):
    assert client.patch("/api/me/memories/nope", json={"title": "x"}).status_code == 404


# ---------------- CAS ----------------
def test_create_conflict_returns_409_with_content(client):
    first = _create(client).json()
    _create(client, description="v2")
    r = _create(client, description="v3", if_version=first["version"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["current_version"] != first["version"]
    assert "v2" in detail["current_content"]


def test_create_with_fresh_version_succeeds(client):
    first = _create(client).json()
    r = _create(client, description="v2", if_version=first["version"])
    assert r.status_code == 200
    assert r.json()["description"] == "v2"


# ---------------- 输入校验 ----------------
def test_bad_kind_rejected(client):
    r = _create(client, kind="nonsense")
    assert r.status_code == 400


def test_bad_slug_rejected(client):
    r = _create(client, slug="../escape")
    assert r.status_code == 400


def test_limit_bounds_enforced(client):
    assert client.get("/api/me/memories?limit=0").status_code == 422
    assert client.get("/api/me/memories?limit=999").status_code == 422


def test_query_filter(client):
    _create(client, slug="pdf", description="关于 PDF 的")
    _create(client, slug="speed", description="关于速度的")
    got = client.get("/api/me/memories?query=PDF").json()
    assert [e["slug"] for e in got["entries"]] == ["pdf"]
