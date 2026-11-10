"""file_glob / file_grep：搜索类工具。

设计取舍：
- 文件枚举优先用 `rg --files`（尊重 .gitignore、比 os.walk 快），不可用时回退 os.walk；
- 内容匹配统一走 Python `re` —— 保证 rg 与回退路径的语义逐字节一致，
  也避免 rg（Rust regex）不支持 lookaround 时静默改判。
- 结果规模交给工具结果预算层，这里只做 head_limit 与目录排除。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from .workspace import PathSecurityError, safe_read_path, user_workspace

EXCLUDE_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".hlaudz", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".idea", ".vscode", ".worktrees",
})

MAX_GLOB_FILES = 100
MAX_CONTEXT_LINES = 20
DEFAULT_HEAD_LIMIT = 250
MAX_FILE_BYTES = 2_000_000  # 超过就不扫，避免把 500MB 日志读进内存


def _root(user_id: str, path: str) -> Path:
    raw = (path or "").strip()
    if not raw or raw in (".", "./"):
        return user_workspace(user_id).resolve()
    return safe_read_path(user_id, raw)


def _rel(root: Path, p: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def _excluded(rel: str) -> bool:
    return any(part in EXCLUDE_DIRS for part in rel.split("/"))


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def _rg_files(root: Path, glob_pat: str | None = None) -> list[Path] | None:
    exe = shutil.which("rg")
    if not exe:
        return None
    cmd = [exe, "--files", "--hidden", "--no-messages"]
    if glob_pat:
        cmd += ["-g", glob_pat]
    cmd.append(".")
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", errors="replace")
    files = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        files.append(root / line)
    return files


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            files.append(Path(dirpath) / name)
    return files


def _candidate_files(root: Path, glob_pat: str | None) -> list[Path]:
    files = _rg_files(root, glob_pat)
    if files is None:
        files = _walk_files(root)
    out: list[Path] = []
    for p in files:
        rel = _rel(root, p)
        if _excluded(rel):
            continue
        if glob_pat and files is not None and glob_pat not in ("*", "**/*"):
            pass  # rg 已按 -g 过滤
        if glob_pat and files is None and not p.match(glob_pat):
            continue
        out.append(p)
    return out


def file_glob(user_id: str, pattern: str, path: str = "") -> str:
    """按文件名/路径模式查找；只回路径，最多 100 个。"""
    try:
        root = _root(user_id, path)
    except PathSecurityError as e:
        return _err(str(e))
    if not root.is_dir():
        return _err(f"不是目录: {path}")
    if not pattern:
        return _err("pattern 不能为空")
    try:
        hits = [
            p for p in root.glob(pattern)
            if p.is_file() and not _excluded(_rel(root, p))
        ]
    except (ValueError, OSError) as e:
        return _err(f"glob 模式无效: {e}")
    total = len(hits)
    sample = sorted(_rel(root, p) for p in hits)[:MAX_GLOB_FILES]
    payload = {"ok": True, "matches": sample, "total": total}
    if total > MAX_GLOB_FILES:
        payload["note"] = f"showing first {MAX_GLOB_FILES} of {total} matches"
    return json.dumps(payload, ensure_ascii=False)


def _read_text(p: Path) -> str | None:
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def file_grep(
    user_id: str,
    pattern: str,
    path: str = "",
    glob: str = "",
    output_mode: str = "content",
    ignore_case: bool = False,
    context: int = 0,
    head_limit: int = DEFAULT_HEAD_LIMIT,
) -> str:
    """在文件内容里搜索；三种输出模式。"""
    try:
        root = _root(user_id, path)
    except PathSecurityError as e:
        return _err(str(e))
    if not pattern:
        return _err("pattern 不能为空")
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return _err(f"invalid regex: {e}")
    if not root.is_dir():
        return _err(f"不是目录: {path}")

    ctx = max(0, min(int(context or 0), MAX_CONTEXT_LINES))
    limit = max(1, int(head_limit or DEFAULT_HEAD_LIMIT))
    import fnmatch

    files = _candidate_files(root, glob or None)
    if glob and _rg_files(root, glob) is None:
        files = [p for p in files if fnmatch.fnmatch(p.name, glob)]

    content_lines: list[str] = []
    matched_files: list[str] = []
    counts: dict[str, int] = {}
    total = 0
    truncated = False

    for p in sorted(files):
        text = _read_text(p)
        if not text:
            continue
        rel = _rel(root, p)
        rows = text.splitlines()
        hits = [i for i, line in enumerate(rows) if rx.search(line)]
        if not hits:
            continue
        matched_files.append(rel)
        counts[rel] = len(hits)
        total += len(hits)
        if output_mode != "content":
            continue
        keep: set[int] = set()
        for i in hits:
            for j in range(max(0, i - ctx), min(len(rows), i + ctx + 1)):
                keep.add(j)
        for i in sorted(keep):
            content_lines.append(f"{rel}:{i + 1}:{rows[i]}")
        if len(content_lines) >= limit:
            content_lines = content_lines[:limit]
            truncated = True
            break

    if output_mode == "files_with_matches":
        return json.dumps({"ok": True, "files": matched_files, "total": total},
                          ensure_ascii=False)
    if output_mode == "count":
        return json.dumps({"ok": True, "counts": counts, "total": total},
                          ensure_ascii=False)
    return json.dumps(
        {"ok": True, "lines": content_lines, "total": total, "truncated": truncated},
        ensure_ascii=False,
    )
