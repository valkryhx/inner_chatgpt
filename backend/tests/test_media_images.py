"""Markdown/workspace 图片：路径归一、鉴权读取、越界拒绝。"""
from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.routers import media
from app import workspace as ws

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z1Z8AAAAASUVORK5CYII="
)


@pytest.fixture()
def user_ws(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "WORKSPACES_DIR", tmp_path / "workspaces")
    root = ws.user_workspace("u_img")
    (root / "images").mkdir(parents=True, exist_ok=True)
    (root / "images" / "chart.png").write_bytes(PNG_1X1)
    (root / "notes.txt").write_text("not an image", encoding="utf-8")
    return root


@pytest.mark.parametrize("raw", [
    "images/chart.png",
    "./images/chart.png",
    "workspace/images/chart.png",
    "/workspace/images/chart.png",
    "/images/chart.png",
])
def test_normalize_and_resolve_relative_forms(user_ws, raw):
    target, media_type = media.resolve_workspace_media("u_img", raw)
    assert target == (user_ws / "images" / "chart.png").resolve()
    assert media_type == "image/png"


def test_normalize_absolute_and_file_uri(user_ws):
    abs_path = str((user_ws / "images" / "chart.png").resolve())
    for raw in (abs_path, "file:///" + abs_path.replace("\\", "/")):
        target, media_type = media.resolve_workspace_media("u_img", raw)
        assert target == (user_ws / "images" / "chart.png").resolve()
        assert media_type == "image/png"


def test_rejects_non_image(user_ws):
    with pytest.raises(Exception) as exc:
        media.resolve_workspace_media("u_img", "notes.txt")
    assert getattr(exc.value, "status_code", None) == 415


def test_rejects_path_escape(user_ws, tmp_path):
    secret = tmp_path / "outside.png"
    secret.write_bytes(PNG_1X1)
    with pytest.raises(Exception) as exc:
        media.resolve_workspace_media("u_img", str(secret.resolve()))
    assert getattr(exc.value, "status_code", None) == 403


def test_image_endpoint_requires_auth(user_ws):
    app = FastAPI()
    app.include_router(media.router)
    with TestClient(app) as client:
        r = client.get("/api/me/workspace-image", params={"path": "images/chart.png"})
    assert r.status_code == 401


def test_image_endpoint_returns_bytes_with_auth(user_ws):
    app = FastAPI()
    app.include_router(media.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "u_img"}
    with TestClient(app) as client:
        r = client.get("/api/me/workspace-image", params={"path": "images/chart.png"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content == PNG_1X1
