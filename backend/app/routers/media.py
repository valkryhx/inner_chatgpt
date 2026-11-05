r"""用户 workspace 媒体文件（图片）鉴权读取。

背景：
- Markdown 里的 ``![alt](chart.png)`` 是相对路径；若直接交给浏览器，会命中
  SPA fallback 返回 index.html，图片自然无法显示。
- ``D:/...`` 这类 Windows 绝对路径会被 react-markdown 的默认 URL 过滤器清空。
- ``<img>`` 标签无法附加 Authorization 请求头。

因此前端遇到 workspace 本地路径时，用 fetch + Bearer token 拉取本接口，
再转成 blob URL 交给 <img>。远程 http(s)/data URL 不经过这里。
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from ..auth import get_current_user
from ..workspace import PathSecurityError, safe_read_path

router = APIRouter(prefix="/api/me", tags=["media"])

MAX_IMAGE_BYTES = 25 * 1024 * 1024
IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
}
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:/")
_FILE_URI_RE = re.compile(r"^file:", re.IGNORECASE)


def normalize_workspace_media_path(raw: str) -> str:
    r"""把模型/用户写出的各种图片路径归一成 workspace 可解析路径。

    支持：
    - ``chart.png`` / ``images/chart.png``
    - ``D:/work/chart.png`` / ``D:\work\chart.png``
    - ``file:///D:/work/chart.png``
    - ``/workspace/images/chart.png``（常见的沙箱别名）
    - ``/images/chart.png``（按 workspace 相对路径处理）
    """
    p = (raw or "").strip()
    if not p:
        raise HTTPException(status_code=400, detail="path 不能为空")

    if _FILE_URI_RE.match(p):
        parsed = urlparse(p)
        p = unquote(parsed.path or "")
    else:
        p = unquote(p)

    # 统一成 POSIX 分隔符；Windows 的 Path 同样接受 forward slashes。
    p = p.replace("\\", "/")

    # file:///D:/... -> D:/...
    if re.match(r"^/[A-Za-z]:/", p):
        p = p[1:]

    # 沙箱别名：/workspace/... 和 workspace/... 都映射到用户 workspace 根。
    for prefix in ("/workspace/", "workspace/"):
        if p.startswith(prefix):
            p = p[len(prefix):]
            break
    else:
        if p.startswith("/") and not _WINDOWS_ABS_RE.match(p):
            # Markdown 中常见的 /images/a.png 按 workspace 相对路径处理。
            p = p.lstrip("/")

    p = p.strip()
    if not p:
        raise HTTPException(status_code=400, detail="path 不能为空")
    return p


def resolve_workspace_media(user_id: str, raw_path: str) -> tuple[Path, str]:
    """解析并校验用户可读的图片文件，返回 (path, media_type)。"""
    p = normalize_workspace_media_path(raw_path)
    try:
        target = safe_read_path(user_id, p)
    except PathSecurityError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"无法解析路径: {exc}") from exc

    if not target.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")

    media_type = IMAGE_MIME_BY_SUFFIX.get(target.suffix.lower())
    if not media_type:
        guessed, _ = mimetypes.guess_type(target.name)
        if not guessed or not guessed.startswith("image/"):
            raise HTTPException(status_code=415, detail="仅支持图片文件")
        media_type = guessed

    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=404, detail="图片不存在") from exc
    if size > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="图片超过 25MB 限制")

    return target, media_type


@router.get("/workspace-image")
async def workspace_image(
    path: str = Query(..., description="workspace 相对路径 / 绝对路径 / file URI"),
    user: dict = Depends(get_current_user),
):
    """按当前用户鉴权读取 workspace 内的图片（供 Markdown 图片渲染使用）。"""
    target, media_type = resolve_workspace_media(user["id"], path)
    headers = {
        "Cache-Control": "private, max-age=60",
        "X-Content-Type-Options": "nosniff",
    }
    if media_type == "image/svg+xml":
        # 即使用于 <img>，也避免直接打开 SVG 时执行脚本/外链。
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return FileResponse(target, media_type=media_type, headers=headers)
