"""Workspace 文件下载：任意格式单文件、目录列表与打包。

图片仍走 ``/api/me/workspace-image``（可内联渲染）；本模块只做「下载」，
因此默认一律 ``Content-Disposition: attachment``，避免 HTML/SVG 在浏览器里执行。
所有路径都通过 ``workspace.resolve_workspace_download`` 解析，只允许当前用户
workspace 内的文件（含子目录），越界/软链逃逸返回 403。
"""
from __future__ import annotations

import mimetypes
import os
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from ..auth import get_current_user
from ..workspace import (
    PathSecurityError,
    resolve_workspace_dir,
    resolve_workspace_download,
    user_workspace,
)

router = APIRouter(prefix="/api/me", tags=["workspace-files"])


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        v = int(str(raw).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


MAX_DOWNLOAD_BYTES = _env_int("WORKSPACE_MAX_DOWNLOAD_MB", 200) * 1024 * 1024
MAX_ZIP_FILES = _env_int("WORKSPACE_MAX_ZIP_FILES", 100)
MAX_ZIP_BYTES = _env_int("WORKSPACE_MAX_ZIP_MB", 500) * 1024 * 1024
LIST_LIMIT_MAX = 1000

# mimetypes 在 Windows 上对 Office 类型不可靠，这里显式补齐常用格式。
MIME_BY_SUFFIX = {
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
}

# 只有明确安全的类型允许 inline 预览；其余一律附件下载。
INLINE_MIME_WHITELIST = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
    "application/pdf",
    "application/yaml",
}


def download_media_type(path: Path) -> str:
    mt = MIME_BY_SUFFIX.get(path.suffix.lower())
    if mt:
        return mt
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _resolve_file(user_id: str, raw: str) -> Path:
    try:
        return resolve_workspace_download(user_id, raw)
    except PathSecurityError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"文件不存在: {raw}") from exc


@router.get("/workspace-file")
async def workspace_file(
    path: str = Query(..., description="workspace 相对路径 / 绝对路径 / file URI"),
    inline: int = Query(0, description="1=浏览器内联（仅白名单类型），默认附件下载"),
    user: dict = Depends(get_current_user),
):
    """按当前用户鉴权下载 workspace 内的任意文件。"""
    target = _resolve_file(user["id"], path)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    if size > MAX_DOWNLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB 下载上限",
        )

    media_type = download_media_type(target)
    inline_ok = bool(inline)
    if inline_ok and media_type.split(";")[0].strip() not in INLINE_MIME_WHITELIST:
        raise HTTPException(status_code=415, detail="该类型不支持内联预览，请使用下载")

    headers = {
        "Cache-Control": "private, max-age=0, must-revalidate",
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(
        target,
        media_type=media_type,
        filename=target.name,
        content_disposition_type="inline" if inline_ok else "attachment",
        headers=headers,
    )


# ---------------- 目录列表 ----------------
def _scan_dir(root: Path, base: Path, depth: int, limit: int, include_hidden: bool):
    entries: list[dict] = []
    truncated = False

    def walk(cur: Path, level: int) -> None:
        nonlocal truncated
        if truncated:
            return
        try:
            items = list(os.scandir(cur))
        except OSError:
            return
        items.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        for item in items:
            if len(entries) >= limit:
                truncated = True
                return
            name = item.name
            if item.is_symlink():
                # 软链可能指向 workspace 之外，列表不展示也不跟随。
                continue
            is_dir = item.is_dir(follow_symlinks=False)
            hidden = name.startswith(".")
            if hidden and not include_hidden and name != ".subagents":
                continue
            try:
                stat = item.stat(follow_symlinks=False)
            except OSError:
                continue
            rel = Path(item.path).relative_to(base).as_posix()
            entries.append({
                "name": name,
                "path": rel,
                "type": "dir" if is_dir else "file",
                "size": 0 if is_dir else int(stat.st_size),
                "mtime": int(stat.st_mtime),
                "hidden": hidden,
            })
            if is_dir and level + 1 < depth:
                walk(Path(item.path), level + 1)

    walk(root, 0)
    return entries, truncated


@router.get("/workspace-files")
async def workspace_files(
    path: str = Query("", description="目录（默认 workspace 根）"),
    depth: int = Query(2, ge=1, le=5),
    limit: int = Query(500, ge=1, le=LIST_LIMIT_MAX),
    include_hidden: int = Query(0, description="1=包含以 . 开头的隐藏文件"),
    user: dict = Depends(get_current_user),
):
    """列出 workspace 目录，供「文件」面板使用。"""
    try:
        root = resolve_workspace_dir(user["id"], path)
    except PathSecurityError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"目录不存在: {path}") from exc

    base = user_workspace(user["id"], create=False)
    entries, truncated = _scan_dir(root, base, depth, limit, bool(include_hidden))
    try:
        rel_root = root.relative_to(base).as_posix()
        if rel_root == ".":
            rel_root = ""
    except ValueError:  # 根目录
        rel_root = ""
    return {
        "root": rel_root,
        "entries": entries,
        "truncated": truncated,
        "limit": limit,
    }


# ---------------- 打包下载 ----------------
class ZipIn(BaseModel):
    paths: list[str] = []


def _unique_arcname(name: str, used: set[str]) -> str:
    """zip 条目名去重，避免 zip slip 与同名覆盖。"""
    arc = name.replace("\\", "/").lstrip("/")
    parts = [p for p in arc.split("/") if p not in ("", ".", "..")]
    arc = "/".join(parts) or "file"
    if arc not in used:
        used.add(arc)
        return arc
    stem, dot, suffix = arc.rpartition(".")
    base = stem if dot else arc
    ext = ("." + suffix) if dot else ""
    i = 2
    while f"{base}-{i}{ext}" in used:
        i += 1
    arc = f"{base}-{i}{ext}"
    used.add(arc)
    return arc


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


@router.post("/workspace-zip")
async def workspace_zip(body: ZipIn, user: dict = Depends(get_current_user)):
    """把选中的文件打包成 zip（最多 100 个 / 500MB）。"""
    paths = [p for p in (body.paths or []) if (p or "").strip()]
    if not paths:
        raise HTTPException(status_code=400, detail="paths 不能为空")
    if len(paths) > MAX_ZIP_FILES:
        raise HTTPException(status_code=413, detail=f"一次最多打包 {MAX_ZIP_FILES} 个文件")

    files: list[Path] = []
    seen: set[str] = set()
    total = 0
    for raw in paths:
        target = _resolve_file(user["id"], raw)
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        try:
            total += target.stat().st_size
        except OSError as exc:
            raise HTTPException(status_code=404, detail=f"文件不存在: {raw}") from exc
        files.append(target)
    if total > MAX_ZIP_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"打包内容超过 {MAX_ZIP_BYTES // (1024 * 1024)}MB 上限",
        )

    base = user_workspace(user["id"], create=False)
    fd, tmp_path = tempfile.mkstemp(prefix="workspace-zip-", suffix=".zip")
    os.close(fd)
    used: set[str] = set()
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for target in files:
                try:
                    arc = target.relative_to(base).as_posix()
                except ValueError:
                    arc = target.name
                zf.write(target, arcname=_unique_arcname(arc, used))
    except BaseException:
        _remove_quiet(tmp_path)
        raise

    name = f"workspace-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=name,
        headers={"Cache-Control": "private, no-store"},
        background=BackgroundTask(_remove_quiet, tmp_path),
    )
