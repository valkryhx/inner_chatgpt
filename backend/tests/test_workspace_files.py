"""Workspace 任意格式下载：单文件、目录列表、打包与安全边界。"""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import workspace as ws
from app.auth import get_current_user
from app.routers import workspace_files


@pytest.fixture()
def user_ws(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "WORKSPACES_DIR", tmp_path / "workspaces")
    root = ws.user_workspace("u_files")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "季度报告-2026Q3.xlsx").write_bytes(b"fake-xlsx-bytes")
    (root / "reports" / "notes.md").write_text("# 说明\n", encoding="utf-8")
    (root / "data.bin").write_bytes(bytes(range(64)))
    (root / ".subagents" / "s1" / "writer").mkdir(parents=True, exist_ok=True)
    (root / ".subagents" / "s1" / "writer" / "out.md").write_text("sub", encoding="utf-8")
    (root / ".secret").write_text("hidden", encoding="utf-8")
    return root


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(workspace_files.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "u_files"}
    return TestClient(app)


def test_requires_auth(user_ws):
    app = FastAPI()
    app.include_router(workspace_files.router)
    with TestClient(app) as client:
        r = client.get("/api/me/workspace-file", params={"path": "data.bin"})
    assert r.status_code == 401


def test_download_unicode_office_file(user_ws):
    with make_client() as client:
        r = client.get("/api/me/workspace-file",
                       params={"path": "reports/季度报告-2026Q3.xlsx"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    disp = r.headers["content-disposition"]
    assert disp.startswith("attachment;")
    assert "filename*=utf-8''" in disp.lower()
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content == b"fake-xlsx-bytes"


def test_download_unknown_type_is_octet_stream(user_ws):
    with make_client() as client:
        r = client.get("/api/me/workspace-file", params={"path": "data.bin"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert "attachment" in r.headers["content-disposition"]


def test_inline_only_for_whitelist(user_ws):
    with make_client() as client:
        ok = client.get("/api/me/workspace-file",
                        params={"path": "reports/notes.md", "inline": 1})
        bad = client.get("/api/me/workspace-file",
                         params={"path": "data.bin", "inline": 1})
    assert ok.status_code == 200
    assert ok.headers["content-disposition"].startswith("inline;")
    assert bad.status_code == 415


def test_rejects_traversal_and_outside_absolute(user_ws, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("x", encoding="utf-8")
    with make_client() as client:
        bad_rel = client.get("/api/me/workspace-file", params={"path": "../secret.txt"})
        bad_abs = client.get("/api/me/workspace-file", params={"path": str(secret)})
    assert bad_rel.status_code in (400, 403)
    assert bad_abs.status_code == 403


def test_missing_file_404(user_ws):
    with make_client() as client:
        r = client.get("/api/me/workspace-file", params={"path": "nope.docx"})
    assert r.status_code == 404


def test_size_limit(user_ws, monkeypatch):
    monkeypatch.setattr(workspace_files, "MAX_DOWNLOAD_BYTES", 4)
    with make_client() as client:
        r = client.get("/api/me/workspace-file", params={"path": "data.bin"})
    assert r.status_code == 413


def test_range_request_supported(user_ws):
    with make_client() as client:
        r = client.get("/api/me/workspace-file", params={"path": "data.bin"},
                       headers={"Range": "bytes=0-3"})
    assert r.status_code == 206
    assert r.content == bytes(range(4))
    assert "content-range" in {k.lower() for k in r.headers}


def test_list_entries_and_hidden(user_ws):
    with make_client() as client:
        r = client.get("/api/me/workspace-files")
        body = r.json()
        hidden = client.get("/api/me/workspace-files", params={"include_hidden": 1}).json()
    assert r.status_code == 200
    paths = {e["path"] for e in body["entries"]}
    assert "reports" in paths
    assert "reports/季度报告-2026Q3.xlsx" in paths
    assert ".subagents" in paths                      # 子代理产物默认可见
    subagent = next(e for e in body["entries"] if e["path"] == ".subagents")
    assert subagent["hidden"] is True
    assert ".secret" not in paths                     # 普通隐藏文件默认不列
    assert ".secret" in {e["path"] for e in hidden["entries"]}


def test_list_depth_limits(user_ws):
    with make_client() as client:
        body = client.get("/api/me/workspace-files", params={"depth": 1}).json()
    paths = {e["path"] for e in body["entries"]}
    assert "reports" in paths
    assert not any(p.startswith("reports/") for p in paths)


def test_zip_download_and_names(user_ws):
    with make_client() as client:
        r = client.post("/api/me/workspace-zip", json={
            "paths": ["reports/notes.md", "reports/季度报告-2026Q3.xlsx", "data.bin"],
        })
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
    assert "reports/notes.md" in names
    assert "reports/季度报告-2026Q3.xlsx" in names
    assert "data.bin" in names
    assert all(".." not in n for n in names)


def test_zip_rejects_invalid_paths(user_ws):
    with make_client() as client:
        bad = client.post("/api/me/workspace-zip",
                          json={"paths": ["reports/notes.md", "../outside.txt"]})
        empty = client.post("/api/me/workspace-zip", json={"paths": []})
    assert bad.status_code in (400, 403)
    assert empty.status_code == 400


def test_zip_file_count_limit(user_ws, monkeypatch):
    monkeypatch.setattr(workspace_files, "MAX_ZIP_FILES", 1)
    with make_client() as client:
        r = client.post("/api/me/workspace-zip",
                        json={"paths": ["reports/notes.md", "data.bin"]})
    assert r.status_code == 413
