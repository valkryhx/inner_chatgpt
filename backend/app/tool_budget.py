"""工具结果预算层：per-tool 上限 → 超限落盘 → 头尾预览 → 可回读路径。

分工：
- 内部截断（各工具自己的 maxlen）负责**性能**：不要把 500MB 读进内存；
- 本模块负责**上下文安全**：读了，但只给模型看这么多，并告诉它全文在哪。
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional

DEFAULT_MAX_RESULT_CHARS = 50_000
DEFAULT_PREVIEW_CHARS = 2_000
PERSIST_LIMIT_CHARS = 500_000

# 与 CC 对齐：Glob 只回路径（上限高），Grep 回内容行（上限低），Bash 居中
TOOL_LIMITS: dict[str, int] = {
    "code_run": 30_000,
    "file_glob": 100_000,
    "file_grep": 20_000,
}


def _int_env(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        return int(v) if v else default
    except (TypeError, ValueError):
        return default


def limit_for(tool_name: str) -> int:
    """工具的字符上限；未注册的工具走全局默认。"""
    default = _int_env("HLZ_TOOL_RESULT_MAX_CHARS", DEFAULT_MAX_RESULT_CHARS)
    return TOOL_LIMITS.get(tool_name, default)


def _persist(content: str, persist_dir: Path) -> Optional[Path]:
    try:
        persist_dir.mkdir(parents=True, exist_ok=True)
        path = persist_dir / f"{uuid.uuid4().hex[:8]}.txt"
        path.write_text(content, encoding="utf-8", newline="\n")
        return path
    except OSError:
        return None


def _preview(content: str, preview_chars: int, note: str) -> str:
    head = content[:preview_chars]
    tail = content[-preview_chars:]
    return f"{head}\n\n{note}\n\n{tail}"


def apply_budget(
    tool_name: str,
    content: str,
    *,
    persist_dir: Optional[Path | str] = None,
) -> str:
    """按工具上限裁剪结果；超限时落盘并返回头尾预览。"""
    if not content:
        return content
    limit = limit_for(tool_name)
    if len(content) <= limit:
        return content

    preview_chars = _int_env("HLZ_TOOL_RESULT_PREVIEW_CHARS", DEFAULT_PREVIEW_CHARS)
    persist_limit = _int_env("HLZ_TOOL_RESULT_PERSIST_LIMIT", PERSIST_LIMIT_CHARS)
    total = len(content)
    truncated = total > persist_limit
    stored = content[:persist_limit] if truncated else content

    omitted = total - preview_chars * 2
    path = _persist(stored, Path(persist_dir)) if persist_dir else None

    if path is None:
        note = (
            f"… [{omitted:,} chars omitted; not persisted] …"
            if not truncated else
            f"… [{omitted:,} chars omitted; output truncated, not persisted] …"
        )
        small = _int_env("HLZ_TOOL_RESULT_FALLBACK_PREVIEW", 4_000)
        return _preview(content, small, note)

    suffix = "；已截断" if truncated else ""
    note = (
        f"… [{omitted:,} chars omitted{suffix}; "
        f"full output: {path.as_posix()} (read with file_read)] …"
    )
    return _preview(content, preview_chars, note)


def budget_dir_for(workspace_root: Path | str) -> Path:
    """落盘目录（必须在用户 workspace 内）。"""
    return Path(workspace_root) / ".hlaudz" / "tool_results"
