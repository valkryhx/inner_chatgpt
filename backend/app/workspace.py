"""Per-user workspace + path-safety helpers.

Every file tool resolves paths through this module so a model can never escape
its own sandbox by using ``..``, an absolute path, or a symlink. The check is
always ``resolve()`` then verify containment under the allowed root.

Layout:
    backend/data/workspaces/<user_id>/      # read + write sandbox
    backend/data/skills/<user_id>/<name>/   # user's private skills (read via skill tools)
    backend/skills_builtin/<name>/          # bundled read-only skills (repo)
    backend/skills_shared/<name>/           # shared public skills (imported bundles, repo)
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import BUILTIN_SKILLS_DIR, SHARED_SKILLS_DIR, SKILLS_DIR, WORKSPACES_DIR

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class PathSecurityError(ValueError):
    """Raised when a requested path escapes its allowed root."""


def _resolve_under(root: Path, candidate: Path) -> Path:
    """Resolve both and assert ``candidate`` stays inside ``root``."""
    root_r = root.resolve()
    try:
        cand_r = candidate.resolve()
    except OSError as exc:  # pragma: no cover - unusual FS errors
        raise PathSecurityError(f"无法解析路径: {candidate}") from exc
    if cand_r != root_r and root_r not in cand_r.parents:
        raise PathSecurityError(f"路径越界（必须位于 {root_r} 内）: {candidate}")
    return cand_r


def user_workspace(user_id: str, *, create: bool = True) -> Path:
    """Return (and optionally create) the user's workspace root."""
    _reject_unsafe_segment(user_id)
    root = WORKSPACES_DIR / user_id
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def user_skills_root(user_id: str, *, create: bool = False) -> Path:
    _reject_unsafe_segment(user_id)
    root = SKILLS_DIR / user_id
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def builtin_skills_root() -> Path:
    return BUILTIN_SKILLS_DIR


def shared_skills_root() -> Path:
    """Public skills every user can use (never edited through the API)."""
    return SHARED_SKILLS_DIR


def write_root_for(user_id: str, root: str = "") -> Path:
    """Resolve (and create) the effective WRITE root inside a user's workspace.

    ``root`` 为空 -> 用户 workspace 根；否则是该根下的一个相对子目录
    （如 ``.subagents/<session>/<agent>``），用于把子代理产物隔离。
    任何越界都会抛 PathSecurityError。
    """
    base = user_workspace(user_id)
    if not (root or "").strip():
        return base
    target = _resolve_under(base, base / root.strip())
    target.mkdir(parents=True, exist_ok=True)
    return target


def safe_workspace_path(user_id: str, rel_path: str, *, root: str = "") -> Path:
    """Resolve a model-supplied path inside the user's workspace (or a sub-root).

    Accepts relative paths (``a/b.txt``) and absolute paths that already point
    inside the effective root; anything else is rejected.
    """
    base = write_root_for(user_id, root)
    raw = (rel_path or "").strip()
    if not raw:
        raise PathSecurityError("path 不能为空")
    p = Path(raw)
    candidate = p if p.is_absolute() else (base / p)
    return _resolve_under(base, candidate)


def safe_read_roots(user_id: str) -> list[Path]:
    """Roots the user may READ from (workspace + all visible skill dirs)."""
    roots = [user_workspace(user_id).resolve()]
    for base in (user_skills_root(user_id), builtin_skills_root(), shared_skills_root()):
        try:
            if base.exists():
                roots.append(base.resolve())
        except OSError:
            continue
    return roots


def safe_read_path(user_id: str, path: str) -> Path:
    """Resolve any readable path: must live under workspace or a skill root."""
    raw = (path or "").strip()
    if not raw:
        raise PathSecurityError("path 不能为空")
    p = Path(raw)
    roots = safe_read_roots(user_id)
    if p.is_absolute():
        candidate = p
    else:
        candidate = roots[0] / p
    try:
        cand_r = candidate.resolve()
    except OSError as exc:  # pragma: no cover
        raise PathSecurityError(f"无法解析路径: {candidate}") from exc
    for root in roots:
        if cand_r == root or root in cand_r.parents:
            return cand_r
    raise PathSecurityError(f"路径越界（必须位于 workspace 或 skill 目录内）: {path}")


def validate_skill_name(name: str) -> str:
    """Validate a skill directory name; returns normalized lowercase name."""
    n = (name or "").strip().lower()
    if not _SKILL_NAME_RE.match(n):
        raise ValueError(
            "Skill 名称只能包含小写字母、数字、'-'、'_'、'.'，以字母或数字开头，长度 1-64"
        )
    if ".." in n or "/" in n or "\\" in n:
        raise ValueError("Skill 名称不能包含路径分隔符")
    return n


def _reject_unsafe_segment(segment: str) -> None:
    if not segment or segment in (".", ".."):
        raise PathSecurityError("非法路径片段")
    if "/" in segment or "\\" in segment:
        raise PathSecurityError("非法路径片段（含分隔符）")
