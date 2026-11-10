"""图片上传与视觉输入：落盘、路径安全、上下文展开、两种 wire_api 的转换。"""
from __future__ import annotations

import base64

import pytest

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    from app import workspace

    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    return tmp_path / "ws"


def test_save_image_writes_into_uploads(ws):
    from app import uploads

    meta = uploads.save_image("u1", "我的截图.png", PNG)
    assert meta["mime"] == "image/png"
    assert meta["path"].startswith("uploads/")
    assert meta["path"].endswith(".png")
    assert meta["size"] == len(PNG)
    assert meta["name"] == "我的截图.png"
    target = ws / "u1" / meta["path"]
    assert target.read_bytes() == PNG


def test_save_image_ignores_client_extension(ws):
    from app import uploads

    # 客户端谎报扩展名：以真实字节为准
    meta = uploads.save_image("u1", "evil.html", PNG)
    assert meta["path"].endswith(".png")


def test_save_image_rejects_non_image(ws):
    from app import uploads

    with pytest.raises(uploads.UploadError) as exc:
        uploads.save_image("u1", "a.txt", b"hello world")
    assert exc.value.status_code == 415


def test_save_image_rejects_oversize(ws, monkeypatch):
    from app import uploads

    monkeypatch.setattr(uploads, "MAX_IMAGE_BYTES", 8)
    with pytest.raises(uploads.UploadError) as exc:
        uploads.save_image("u1", "big.png", PNG)
    assert exc.value.status_code == 413


def test_resolve_attachment_rejects_traversal(ws):
    from app import uploads

    meta = uploads.save_image("u1", "a.png", PNG)
    ok = uploads.resolve_attachment("u1", meta["path"])
    assert ok.exists()

    for bad in ("../u2/uploads/x.png", "uploads/../../evil.png", "/etc/passwd", "notes.txt"):
        with pytest.raises(uploads.UploadError):
            uploads.resolve_attachment("u1", bad)


def test_to_data_url(ws):
    from app import uploads

    meta = uploads.save_image("u1", "a.png", PNG)
    path = uploads.resolve_attachment("u1", meta["path"])
    url = uploads.to_data_url(path, meta["mime"])
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG


def _user_msg(path, text="看图"):
    return {"role": "user", "content": text,
            "attachments": [{"path": path, "mime": "image/png", "name": "a.png"}]}


def test_expand_images_keeps_history_clean(ws):
    from app import uploads

    meta = uploads.save_image("u1", "a.png", PNG)
    history = [_user_msg(meta["path"])]
    wire = uploads.expand_images_for_wire(history, "u1")
    assert wire[0]["image_urls"][0].startswith("data:image/png;base64,")
    # 原 history 不能被写入 base64（否则会被持久化进 session_history）
    assert "image_urls" not in history[0]


def test_expand_images_limits_to_recent_rounds(ws):
    from app import uploads

    meta = uploads.save_image("u1", "a.png", PNG)
    history = []
    for i in range(5):
        history.append(_user_msg(meta["path"], f"q{i}"))
        history.append({"role": "assistant", "content": f"a{i}"})
    wire = uploads.expand_images_for_wire(history, "u1", history_rounds=2)
    with_images = [m for m in wire if m.get("image_urls")]
    assert len(with_images) == 2
    # 最近的两次带图
    assert with_images[-1]["content"] == "q4"
    assert with_images[0]["content"] == "q3"


def test_expand_images_skips_missing_file(ws):
    from app import uploads

    history = [_user_msg("uploads/not-there.png")]
    wire = uploads.expand_images_for_wire(history, "u1")
    assert not wire[0].get("image_urls")


def test_chat_wire_format_has_image_parts(ws):
    from app.llm_client import _to_chat_messages

    msgs = [{"role": "user", "content": "看图", "image_urls": ["data:image/png;base64,AAA"]}]
    out = _to_chat_messages(msgs, "")
    content = out[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看图"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_chat_wire_format_unchanged_without_images():
    from app.llm_client import _to_chat_messages

    out = _to_chat_messages([{"role": "user", "content": "纯文本"}], "")
    assert out[0]["content"] == "纯文本"


def test_responses_wire_format_has_input_image():
    from app.llm_client import _to_responses_input

    msgs = [{"role": "user", "content": "看图", "image_urls": ["data:image/png;base64,AAA"]}]
    out = _to_responses_input(msgs)
    parts = out[0]["content"]
    assert parts[0] == {"type": "input_text", "text": "看图"}
    assert parts[1]["type"] == "input_image"
    assert parts[1]["image_url"].startswith("data:image/png;base64,")


def test_upload_endpoint_roundtrip(ws):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.auth import get_current_user
    from app.routers import media

    app = FastAPI()
    app.include_router(media.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}

    with TestClient(app) as client:
        ok = client.post("/api/me/uploads", files={"file": ("我的图.png", PNG, "image/png")})
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["path"].startswith("uploads/")
        assert body["url"].endswith(body["path"])
        assert (ws / "u1" / body["path"]).exists()

        bad = client.post("/api/me/uploads", files={"file": ("a.txt", b"not an image", "text/plain")})
        assert bad.status_code == 415


def test_send_message_schema_accepts_attachments():
    from app.schemas import SendMessageIn

    body = SendMessageIn(text="", attachments=[{"path": "uploads/a.png"}])
    assert body.attachments[0].path == "uploads/a.png"


def test_expand_images_survives_empty_text(ws):
    from app import uploads

    meta = uploads.save_image("u1", "a.png", PNG)
    wire = uploads.expand_images_for_wire([_user_msg(meta["path"], "")], "u1")
    assert wire[0]["image_urls"]
