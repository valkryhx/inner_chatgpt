"""模型探测端点：形状解析、错误处理、用户隔离。

核心回归：前端曾硬编码 qwen3-max 这类短名，在湛卢上不存在，
导致 403「权限不足」。模型名必须来自端点自己的 /models。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, history_store
from app.auth import get_current_user
from app.model_discovery import _candidate_urls, _extract_ids, list_remote_models
from app.routers import me as me_router


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(history_store, "DB_PATH", path)
    run(db.init_db())
    yield path


def make_client(uid: str) -> TestClient:
    app = FastAPI()
    app.include_router(me_router.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": uid}
    return TestClient(app)


# ---------------- URL 候选 ----------------

def test_candidate_urls_plain_host():
    urls = _candidate_urls("https://api.example.com")
    assert "https://api.example.com/models" in urls
    assert "https://api.example.com/v1/models" in urls


def test_candidate_urls_with_v1_suffix():
    urls = _candidate_urls("https://api.example.com/v1")
    assert urls[0] == "https://api.example.com/v1/models"
    # 已带 /v1 就不该再拼出 /v1/v1
    assert not any("/v1/v1" in u for u in urls)


def test_candidate_urls_trailing_slash():
    assert _candidate_urls("https://h.com/v1/")[0] == "https://h.com/v1/models"


def test_candidate_urls_empty():
    assert _candidate_urls("") == []
    assert _candidate_urls("   ") == []


# ---------------- 响应形状兼容 ----------------

@pytest.mark.parametrize("payload,expected", [
    ({"data": [{"id": "m1"}, {"id": "m2"}]}, ["m1", "m2"]),
    ({"models": [{"id": "a"}, {"id": "b"}]}, ["a", "b"]),
    ({"result": [{"id": "x"}]}, ["x"]),
    (["z1", "z2"], ["z1", "z2"]),
    ({"data": [{"model": "via-model-key"}]}, ["via-model-key"]),
    ({"data": [{"name": "via-name"}]}, ["via-name"]),
])
def test_extract_ids_shapes(payload, expected):
    ids = [m["id"] for m in _extract_ids(payload)]
    assert ids == expected


def test_extract_ids_dedup_and_sort():
    payload = {"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}]}
    assert [m["id"] for m in _extract_ids(payload)] == ["a", "b"]


def test_extract_ids_ignores_garbage():
    payload = {"data": [None, 123, {}, {"id": ""}, {"id": "ok"}]}
    assert [m["id"] for m in _extract_ids(payload)] == ["ok"]


def test_extract_ids_empty_payload():
    for p in [None, {}, {"data": []}, {"data": "notalist"}]:
        assert _extract_ids(p) == []


# ---------------- list_remote_models 的输入校验 ----------------

def test_missing_base_url():
    r = run(list_remote_models("", "sk-x"))
    assert r["ok"] is False
    assert "base_url" in r["error"]


def test_missing_api_key():
    r = run(list_remote_models("https://h.com", ""))
    assert r["ok"] is False
    assert "api_key" in r["error"]


# ---------------- HTTP 错误处理不抛异常 ----------------

def _mock_resp(status: int, json_body=None, text: str = ""):
    req = httpx.Request("GET", "https://h.com/models")
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=req)
    return httpx.Response(status, text=text, request=req)


def test_401_returns_readable_hint():
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_mock_resp(401))):
        r = run(list_remote_models("https://h.com", "sk-bad"))
    assert r["ok"] is False
    assert "401" in r["error"]
    assert "手填" in r["hint"] or "Base URL" in r["hint"]


def test_500_falls_through_all_candidates():
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_mock_resp(500, text="boom"))):
        r = run(list_remote_models("https://h.com", "sk-x"))
    assert r["ok"] is False
    assert r["models"] == []


def test_network_error_does_not_raise():
    async def boom(*a, **k):
        raise httpx.ConnectError("no route")
    with patch("httpx.AsyncClient.get", new=boom):
        r = run(list_remote_models("https://h.com", "sk-x"))
    assert r["ok"] is False
    assert "ConnectError" in r["error"] or "no route" in r["error"]


def test_success_returns_models():
    body = {"data": [{"id": "moma/qwen3.7-max", "owned_by": "qwen"}]}
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_mock_resp(200, body))):
        r = run(list_remote_models("https://h.com/v1", "sk-x"))
    assert r["ok"] is True
    assert r["models"][0]["id"] == "moma/qwen3.7-max"
    assert r["models"][0]["owned_by"] == "qwen"


def test_200_but_no_models_is_failure():
    """返回 200 但没有可用模型 ID，不能当成成功。"""
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_mock_resp(200, {"data": []}))):
        r = run(list_remote_models("https://h.com", "sk-x"))
    assert r["ok"] is False


# ---------------- 路由层 ----------------

def test_route_requires_auth():
    from app.routers import me as m
    app = FastAPI()
    app.include_router(m.router)
    with TestClient(app) as c:
        resp = c.post("/api/me/llm-models-probe", json={"base_url": "https://h.com", "api_key": "k"})
    assert resp.status_code == 401


def test_route_uses_stored_key_when_config_id_given(isolated_db):
    """编辑既有配置时前端不传 key（只显示掩码），后端按 user_id 取库里的。"""
    cfg = run(db.create_llm_config(
        "u1", label="t", base_url="https://gw.example/v1", api_key="sk-STORED",
        model="m1", wire_format="", wire_api="openai_chat",
    ))
    captured = {}

    async def fake_get(self, url, headers=None, **kw):
        captured["url"] = url
        captured["auth"] = (headers or {}).get("Authorization")
        return _mock_resp(200, {"data": [{"id": "real-model"}]})

    with patch("httpx.AsyncClient.get", new=fake_get):
        with make_client("u1") as client:
            resp = client.post("/api/me/llm-models-probe",
                               json={"config_id": cfg["id"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["models"][0]["id"] == "real-model"
    assert captured["auth"] == "Bearer sk-STORED"


def test_route_cannot_use_another_users_config(isolated_db):
    """传别人的 config_id 不能借此拿到他的 key。"""
    cfg = run(db.create_llm_config(
        "u2", label="other", base_url="https://gw.example/v1", api_key="sk-OTHER",
        model="m1", wire_format="", wire_api="openai_chat",
    ))
    with make_client("u1") as client:
        resp = client.post("/api/me/llm-models-probe", json={"config_id": cfg["id"]})
    assert resp.status_code == 404