"""Filesystem-backed Skill store (SKILL.md), per-user isolated.

Two roots:
  - builtin:  backend/skills_builtin/<name>/SKILL.md   (read-only, all users)
  - user:     backend/data/skills/<user_id>/<name>/SKILL.md  (per user, writable)

A user skill with the same name overrides the builtin one. Names are validated
and every resolved path is checked to stay inside its root (no ``..`` / symlink
escape). See ``docs/20261105-Skill与基础工具设计与实现.md`` for the design.

Format (compatible with Claude Code / Codex / GenericAgent):

    ---
    name: pdf-report
    description: 把数据整理成 PDF 报告
    ---
    # body ...
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .workspace import (
    PathSecurityError,
    builtin_skills_root,
    shared_skills_root,
    user_skills_root,
    validate_skill_name,
)

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$")
_LIST_RE = re.compile(r"^\s*-\s*(.+?)\s*$")


@dataclass
class SkillSpec:
    name: str
    description: str
    source: str  # "builtin" | "user"
    root: Path
    path: Path
    body: str
    when_to_use: str = ""
    allowed_tools: tuple[str, ...] = ()
    version: str = ""
    disable_model_invocation: bool = False
    user_invocable: bool = True

    @property
    def chars(self) -> int:
        return len(self.body or "")


def _parse_scalar(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        inner = v[1:-1]
        if v[0] == '"':
            # Undo the escaping applied by _render_document.
            inner = inner.replace(chr(92) + '"', '"').replace(chr(92) * 2, chr(92))
        return inner
    return v


def _parse_list(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(x).strip() for x in value if str(x).strip())
    if isinstance(value, str):
        return tuple(p.strip() for p in re.split(r"[,;\n]", value) if p.strip())
    return ()


def _parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    t = str(value or "").strip().lower()
    if t in {"1", "true", "yes", "on"}:
        return True
    if t in {"0", "false", "no", "off"}:
        return False
    return default


def _heading_description(markdown: str) -> str:
    for line in markdown.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()[:250]
        if s:
            return s[:250]
    return ""


def parse_skill_markdown(text: str, fallback_name: str) -> tuple[dict, str]:
    """Split a SKILL.md into (frontmatter dict, body). Missing frontmatter is OK."""
    m = FRONTMATTER_RE.match(text or "")
    if not m:
        return {"name": fallback_name, "description": _heading_description(text or "")}, text or ""
    fm, body = m.group(1), text[m.end():]
    data: dict = {}
    current: Optional[str] = None
    for raw in fm.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = _LIST_RE.match(line)
        if item and current:
            existing = data.setdefault(current, [])
            if isinstance(existing, list):
                existing.append(_parse_scalar(item.group(1)))
            continue
        kv = _NAME_RE.match(line)
        if kv:
            current = kv.group(1).replace("-", "_")
            value = kv.group(2)
            data[current] = _parse_scalar(value) if value else []
    data.setdefault("name", fallback_name)
    data.setdefault("description", _heading_description(body))
    return data, body


def _read_spec(skill_file: Path, source: str) -> Optional[SkillSpec]:
    try:
        text = skill_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fallback = skill_file.parent.name
    meta, body = parse_skill_markdown(text, fallback)
    name = str(meta.get("name") or fallback).strip() or fallback
    description = str(meta.get("description") or "").strip()
    return SkillSpec(
        name=name,
        description=description,
        source=source,
        root=skill_file.parent,
        path=skill_file,
        body=body,
        when_to_use=str(meta.get("when_to_use") or meta.get("when") or "").strip(),
        allowed_tools=_parse_list(meta.get("allowed_tools") or meta.get("allowed")),
        version=str(meta.get("version") or "").strip(),
        disable_model_invocation=_parse_bool(meta.get("disable_model_invocation")),
        user_invocable=_parse_bool(meta.get("user_invocable"), True),
    )


def _scan(root: Path, source: str) -> dict[str, SkillSpec]:
    out: dict[str, SkillSpec] = {}
    if not root.exists():
        return out
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return out
    for child in children:
        if not child.is_dir():
            continue
        f = child / "SKILL.md"
        if not f.is_file():
            continue
        spec = _read_spec(f, source)
        if spec is None:
            continue
        out[spec.name.lower()] = spec
    return out


def list_skills(user_id: str) -> list[SkillSpec]:
    """All skills visible to a user.

    Precedence (later wins): builtin -> shared -> user.
    """
    merged: dict[str, SkillSpec] = _scan(builtin_skills_root(), "builtin")
    merged.update(_scan(shared_skills_root(), "shared"))
    merged.update(_scan(user_skills_root(user_id), "user"))
    return sorted(merged.values(), key=lambda s: s.name.lower())


def list_builtin_skills() -> list[SkillSpec]:
    return sorted(_scan(builtin_skills_root(), "builtin").values(), key=lambda s: s.name.lower())


def list_shared_skills() -> list[SkillSpec]:
    return sorted(_scan(shared_skills_root(), "shared").values(), key=lambda s: s.name.lower())


def _readonly_roots() -> tuple[Path, ...]:
    return (shared_skills_root(), builtin_skills_root())


def get_skill(user_id: str, name: str) -> Optional[SkillSpec]:
    try:
        key = validate_skill_name(name)
    except ValueError:
        # Reject traversal / illegal names instead of resolving arbitrary paths.
        return None
    user_root = user_skills_root(user_id)
    if user_root.exists():
        f = user_root / key / "SKILL.md"
        if f.is_file():
            return _read_spec(f, "user")
    for root, source in ((shared_skills_root(), "shared"), (builtin_skills_root(), "builtin")):
        f = root / key / "SKILL.md"
        if f.is_file():
            return _read_spec(f, source)
    return None


def load_skill_content(user_id: str, name: str, args: str = "") -> dict:
    """Return the rendered skill content for the model (raises KeyError if unknown)."""
    spec = get_skill(user_id, name)
    if spec is None:
        available = ", ".join(s.name for s in list_skills(user_id)) or "(无)"
        raise KeyError(f"未知 Skill: {name}。可用: {available}")
    base_dir = str(spec.root.resolve())
    body = (spec.body or "").replace("${SKILL_DIR}", base_dir.replace("\\", "/"))
    body = body.replace("${CLAUDE_SKILL_DIR}", base_dir)
    body = body.replace("${GA_SKILL_DIR}", base_dir)
    body = body.replace("$ARGUMENTS", args or "")
    content = (
        f"Skill: {spec.name}\n"
        f"Base directory: {base_dir}\n\n"
        f"{body}"
    )
    return {
        "name": spec.name,
        "description": spec.description,
        "source": spec.source,
        "path": str(spec.path),
        "base_dir": base_dir,
        "allowed_tools": list(spec.allowed_tools),
        "content": content,
    }


# ---------------- user private skill CRUD ----------------

def _render_document(name: str, description: str, body: str) -> str:
    """Build a SKILL.md document with a minimal YAML frontmatter.

    The description is quoted so characters like ':' / '#' / a leading
    '-' cannot break YAML parsing on the next read.
    """
    desc = (description or "").strip().replace(chr(10), " ")
    lines = ["---", f"name: {name}"]
    if desc:
        safe = desc.replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))
        lines.append("description: " + chr(34) + safe + chr(34))
    lines.append("---")
    lines.append("")
    return chr(10).join(lines) + chr(10) + (body or "").rstrip() + chr(10)
def _user_skill_dir(user_id: str, name: str, *, create: bool = False) -> Path:
    safe = validate_skill_name(name)
    root = user_skills_root(user_id, create=create)
    target = (root / safe)
    # Defence in depth: ensure resolved dir stays under the user's root.
    root_r = root.resolve()
    cand = target.resolve()
    if cand != root_r and root_r not in cand.parents:
        raise PathSecurityError(f"Skill 路径越界: {name}")
    return target


def save_user_skill(user_id: str, name: str, description: str, body: str) -> SkillSpec:
    """Create or overwrite a private skill. Returns the parsed spec."""
    safe = validate_skill_name(name)
    target = _user_skill_dir(user_id, safe, create=True)
    target.mkdir(parents=True, exist_ok=True)
    doc = _render_document(safe, description, body)
    f = target / "SKILL.md"
    with open(f, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(doc)
    spec = _read_spec(f, "user")
    assert spec is not None
    return spec


def delete_user_skill(user_id: str, name: str) -> bool:
    """Delete a private skill directory (including bundled files).

    The target is resolved and re-checked to stay strictly under the user's skill
    root before any recursive removal, so a crafted name cannot escape.
    """
    import shutil

    safe = validate_skill_name(name)
    target = _user_skill_dir(user_id, safe)
    if not target.is_dir():
        return False
    root_r = user_skills_root(user_id).resolve()
    target_r = target.resolve()
    if target_r == root_r or root_r not in target_r.parents:
        raise PathSecurityError(f"Skill 路径越界: {name}")
    shutil.rmtree(target_r, ignore_errors=True)
    return True


# ---------------- skill bundle import (zip) ----------------

MAX_BUNDLE_BYTES = 20 * 1024 * 1024          # compressed upload cap
MAX_BUNDLE_UNCOMPRESSED = 60 * 1024 * 1024   # zip-bomb guard
MAX_BUNDLE_FILES = 500


class BundleError(ValueError):
    """Raised when an uploaded skill bundle is invalid or unsafe."""


def _safe_extract_zip(data: bytes, dest: Path) -> None:
    """Extract a zip into ``dest`` with traversal / symlink / bomb guards.

    Rejects: absolute member paths, ``..`` segments, symlink members, too many
    files, and an uncompressed total above ``MAX_BUNDLE_UNCOMPRESSED``.
    """
    import io as _io
    import zipfile

    if len(data) > MAX_BUNDLE_BYTES:
        raise BundleError(f"压缩包过大（上限 {MAX_BUNDLE_BYTES // (1024 * 1024)}MB）")
    try:
        zf = zipfile.ZipFile(_io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise BundleError("不是有效的 zip 文件") from e

    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_BUNDLE_FILES:
        raise BundleError(f"文件数过多（上限 {MAX_BUNDLE_FILES}）")
    total = sum(i.file_size for i in infos)
    if total > MAX_BUNDLE_UNCOMPRESSED:
        raise BundleError("解压后体积过大，疑似 zip 炸弹")

    dest_r = dest.resolve()
    for info in infos:
        raw = info.filename.replace(chr(92), "/")
        # symlink: unix mode high bits 0o120000
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise BundleError(f"包含符号链接，拒绝导入: {raw}")
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts):
            raise BundleError(f"包含非法路径: {raw}")
        target = dest_r.joinpath(*parts)
        if target != dest_r and dest_r not in target.resolve().parents:
            raise BundleError(f"路径越界: {raw}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            while True:
                chunk = src.read(65536)
                if not chunk:
                    break
                out.write(chunk)


def _locate_skill_dirs(root: Path) -> list[Path]:
    """Find skill directories inside an extracted bundle.

    Accepts either a single skill at the archive root, or one-or-more skills as
    immediate subdirectories (the common multi-skill bundle layout).
    """
    if (root / "SKILL.md").is_file():
        return [root]
    found: list[Path] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir() and (child / "SKILL.md").is_file():
            found.append(child)
    if found:
        return found
    # Fall back to a bounded deep search (git archives sometimes nest one level).
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        for grand in sorted(child.iterdir(), key=lambda p: p.name.lower()):
            if grand.is_dir() and (grand / "SKILL.md").is_file():
                found.append(grand)
    return found


def _normalize_skill_frontmatter(skill_dir: Path, dir_name: str) -> None:
    """Make the frontmatter ``name`` a valid, loadable skill name.

    Imported skills often have a human-readable (e.g. Chinese) ``name:`` in the
    frontmatter while the directory is slug-shaped. The loader validates names
    against ``[a-z0-9._-]``, so a Chinese name would be listed but unloadable.
    Rewrite just that one line to the directory slug and keep everything else.
    """
    f = skill_dir / "SKILL.md"
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return
    m = FRONTMATTER_RE.match(text)
    if not m:
        return
    fm = m.group(1)
    lines = fm.splitlines()
    changed = False
    for i, line in enumerate(lines):
        kv = _NAME_RE.match(line)
        if kv and kv.group(1).replace("-", "_") == "name":
            if _parse_scalar(kv.group(2)) != dir_name:
                lines[i] = f"name: {dir_name}"
                changed = True
            break
    if not changed:
        return
    NL = chr(10)
    new_text = "---" + NL + NL.join(lines) + NL + "---" + NL + text[m.end():]
    with open(f, "w", encoding="utf-8", newline=NL) as fh:
        fh.write(new_text)


def import_skill_bundle(data: bytes, target_root: Path, *, overwrite: bool = False,
                        default_name: str = "") -> list[str]:
    """Install a zip of one or more skill directories into ``target_root``.

    ``default_name`` names the skill when the archive root itself is the skill
    (no wrapping folder); the API passes the uploaded filename stem for that.

    Returns the list of installed skill directory names. Raises ``BundleError``
    for unsafe/invalid archives. Existing skills are only replaced when
    ``overwrite`` is True.
    """
    import shutil
    import tempfile

    target_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="skillbundle_"))
    try:
        _safe_extract_zip(data, tmp)
        dirs = _locate_skill_dirs(tmp)
        if not dirs:
            raise BundleError("压缩包里没有找到 SKILL.md（需位于根目录或一级子目录）")
        installed: list[str] = []
        seen: set[str] = set()
        for d in dirs:
            raw_name = d.name if d != tmp else (default_name or "skill")
            try:
                name = validate_skill_name(raw_name)
            except ValueError as e:
                raise BundleError(f"Skill 目录名不合法（{raw_name}）：{e}") from e
            if name in seen:
                raise BundleError(f"压缩包里有重名 Skill: {name}")
            seen.add(name)
            dest = target_root / name
            if dest.exists():
                if not overwrite:
                    raise BundleError(f"已存在同名 Skill: {name}（如需替换请选择覆盖）")
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(d, dest)
            # Normalise AFTER copying so a root-level SKILL.md is covered too.
            _normalize_skill_frontmatter(dest, name)
            installed.append(name)
        return installed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def install_user_skill_bundle(user_id: str, data: bytes, *, overwrite: bool = False,
                             default_name: str = "") -> list[str]:
    return import_skill_bundle(data, user_skills_root(user_id, create=True),
                               overwrite=overwrite, default_name=default_name)


def install_shared_skill_bundle(data: bytes, *, overwrite: bool = False,
                                default_name: str = "") -> list[str]:
    """Install into the repo-shipped shared skills root (operator action)."""
    return import_skill_bundle(data, shared_skills_root(), overwrite=overwrite,
                               default_name=default_name)


def is_builtin_skill(name: str) -> bool:
    """True when the name exists in a read-only root (builtin or shared).

    Despite the historical name this now also covers the shared/public root,
    because both are read-only through the API: a user must create a private
    skill of the same name to override them.
    """
    safe = validate_skill_name(name)
    for root in _readonly_roots():
        if (root / safe / "SKILL.md").is_file():
            return True
    return False


def format_skill_listing(skills: Iterable[SkillSpec], char_budget: int = 8000) -> str:
    """L1 index: one line per model-invocable skill, with a char budget."""
    lines: list[str] = []
    used = 0
    for s in skills:
        if s.disable_model_invocation:
            continue
        desc = re.sub(r"\s+", " ", s.description or "").strip()
        if s.when_to_use:
            desc = f"{desc} - {s.when_to_use}" if desc else s.when_to_use
        if len(desc) > 250:
            desc = desc[:249].rstrip() + "..."
        line = f"- {s.name}: {desc}" if desc else f"- {s.name}"
        extra = len(line) + (1 if lines else 0)
        if lines and used + extra > char_budget:
            compact = f"- {s.name}"
            if used + len(compact) + 1 <= char_budget:
                lines.append(compact)
                used += len(compact) + 1
                continue
            break
        lines.append(line)
        used += extra
    return "\n".join(lines)


def build_skill_prompt(user_id: str, char_budget: int = 8000) -> str:
    """The L1 listing to append to the system prompt (empty string if none)."""
    listing = format_skill_listing(list_skills(user_id), char_budget=char_budget)
    if not listing:
        return ""
    return (
        "\n\n[Available Skills]\n"
        "当用户任务与下面某个 Skill 匹配时，先调用 load_skill 加载它的完整说明再执行。"
        "这里只是索引，不要凭索引直接臆测步骤。\n"
        f"{listing}"
    )
