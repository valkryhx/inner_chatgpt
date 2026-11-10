"""用户上传的图片：落盘到 workspace/uploads/，消息里只存引用。

为什么不进数据库：
- 单张上限 25MB，base64 写进 messages 会让 DB / WAL 迅速膨胀、备份变慢；
- workspace 已经按用户隔离，并且有鉴权读取接口（``/api/me/workspace-image``），
  前端缩略图不用再写一套；
- 模型还能用 ``code_run`` / ``list_files`` 二次处理这些文件。

送模型时才把文件读成 data URL（见 ``expand_images_for_wire``），历史里永远不存 base64。
"""
from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any, Optional

from . import workspace

UPLOAD_SUBDIR = "uploads"
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 4
IMAGE_HISTORY_ROUNDS = 3

_SUFFIX_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
}
_MIME_BY_SUFFIX = {v: k for k, v in _SUFFIX_BY_MIME.items()}
# 公开别名：路由层补全 mime 时要用
MIME_BY_SUFFIX = _MIME_BY_SUFFIX


class UploadError(Exception):
    """带 HTTP 语义的上传错误，由路由层原样转成状态码。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def sniff_image_mime(data: bytes) -> Optional[str]:
    """按 magic bytes 判类型；客户端给的扩展名不可信。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    head = data[:256].lower()
    if b"<svg" in head:
        return "image/svg+xml"
    return None


def _uploads_root(user_id: str) -> Path:
    return workspace.user_workspace(user_id) / UPLOAD_SUBDIR


def save_image(user_id: str, filename: str, data: bytes) -> dict[str, Any]:
    if not data:
        raise UploadError(400, "空文件")
    if len(data) > MAX_IMAGE_BYTES:
        raise UploadError(413, f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB 限制")
    mime = sniff_image_mime(data)
    if not mime:
        raise UploadError(415, "仅支持图片文件")
    root = _uploads_root(user_id)
    root.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{_SUFFIX_BY_MIME[mime]}"
    (root / stored).write_bytes(data)
    return {
        "path": f"{UPLOAD_SUBDIR}/{stored}",
        "name": (filename or stored)[:120],
        "mime": mime,
        "size": len(data),
    }


def resolve_attachment(user_id: str, rel_path: Any) -> Path:
    rel = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
    if not rel.startswith(UPLOAD_SUBDIR + "/"):
        raise UploadError(403, "只允许引用 uploads/ 下的图片")
    try:
        path = workspace.safe_workspace_path(user_id, rel)
    except workspace.PathSecurityError:
        raise UploadError(403, "路径越界") from None
    if not path.exists() or not path.is_file():
        raise UploadError(404, "图片不存在")
    return path


def to_data_url(path: Path, mime: str = "") -> str:
    kind = (mime or "").strip() or _MIME_BY_SUFFIX.get(path.suffix.lower(), "")
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{kind or 'application/octet-stream'};base64,{payload}"


def expand_images_for_wire(messages: list[dict[str, Any]], user_id: str, *,
                           history_rounds: int = IMAGE_HISTORY_ROUNDS) -> list[dict[str, Any]]:
    """把最近若干轮用户消息里的图片读成 data URL，返回**副本**。

    原 history 不能写 image_urls：它会被持久化进 session_history，base64 会把上下文撑爆。
    """
    wire = [dict(m) for m in messages]
    idxs = [i for i, m in enumerate(wire) if m.get("role") == "user" and m.get("attachments")]
    keep = set(idxs[-history_rounds:]) if history_rounds > 0 else set()
    for i in idxs:
        if i not in keep:
            continue
        urls: list[str] = []
        for att in (wire[i].get("attachments") or [])[:MAX_IMAGES_PER_MESSAGE]:
            try:
                path = resolve_attachment(user_id, (att or {}).get("path"))
            except UploadError:
                continue
            urls.append(to_data_url(path, str((att or {}).get("mime") or "")))
        if urls:
            wire[i]["image_urls"] = urls
    return wire
