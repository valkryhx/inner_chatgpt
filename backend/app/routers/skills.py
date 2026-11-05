"""Skill 路由：用户私有 Skill 的 CRUD + 内置 Skill 列表（按 user_id 隔离）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from ..auth import get_current_user
from ..schemas import SkillIn, SkillOut, SkillUpdate
from ..skill_store import (
    BundleError,
    delete_user_skill,
    install_shared_skill_bundle,
    install_user_skill_bundle,
    is_builtin_skill,
    list_builtin_skills,
    list_shared_skills,
    list_skills,
    parse_skill_markdown,
    save_user_skill,
)

router = APIRouter(prefix="/api", tags=["skills"])


def _updated_at(spec) -> int:
    try:
        return int(spec.path.stat().st_mtime)
    except OSError:
        return 0


def _file_count(spec) -> int:
    """Count files in the skill dir (bundled refs/scripts), capped for safety."""
    try:
        root = spec.root
        n = 0
        for p in root.rglob("*"):
            if p.is_file():
                n += 1
                if n >= 999:
                    break
        return n
    except OSError:
        return 0


def _out(spec) -> SkillOut:
    return SkillOut(
        name=spec.name,
        description=spec.description,
        source=spec.source,
        chars=spec.chars,
        updated_at=_updated_at(spec),
        model_invocable=not spec.disable_model_invocation,
        files=_file_count(spec),
        readonly=spec.source in ("builtin", "shared"),
    )


def _resolve_body(body: SkillIn | SkillUpdate, current_body: str = "", current_desc: str = ""):
    """Return (name, description, content). `document` overrides content."""
    document = getattr(body, "document", None)
    if document:
        meta, parsed_body = parse_skill_markdown(document, fallback_name=getattr(body, "name", "") or "skill")
        name = str(meta.get("name") or getattr(body, "name", "") or "").strip()
        desc = str(meta.get("description") or current_desc).strip()
        return name, desc, parsed_body
    name = (getattr(body, "name", None) or "").strip()
    desc = getattr(body, "description", None)
    content = getattr(body, "content", None)
    return name, (desc if desc is not None else current_desc), (content if content is not None else current_body)


async def _invalidate(user_id: str) -> None:
    """Invalidate cached runtime so the new Skill index is picked up next turn."""
    from ..runtime import get_hub
    hub = get_hub()
    if hub:
        await hub.invalidate(user_id)


@router.get("/skills", response_model=list[SkillOut])
async def list_builtin(user: dict = Depends(get_current_user)):
    """内置 Skill（只读，所有用户可见）。"""
    return [_out(s) for s in list_builtin_skills()]


def _upload_default_name(filename: str | None) -> str:
    """Derive a skill name from the uploaded zip filename.

    ``技术报告.zip`` -> ``技术报告``. Only lowercased ASCII/digits/-/_/. survive
    validate_skill_name, so we slugify: non-ASCII becomes ``-`` and we fall back
    to ``skill`` when nothing is left.
    """
    import re
    stem = (filename or "").rsplit("/", 1)[-1]
    if stem.lower().endswith(".zip"):
        stem = stem[:-4]
    slug = re.sub(r"[^a-z0-9._-]+", "-", stem.lower()).strip("-._")
    return slug or "skill"


@router.get("/skills/shared", response_model=list[SkillOut])
async def list_shared(user: dict = Depends(get_current_user)):
    """公用 Skill（所有用户可见，只读；可含脚本/参考资料等完整目录）。"""
    return [_out(s) for s in list_shared_skills()]


@router.post("/me/skills/import")
async def import_skill(
    file: UploadFile = File(...),
    overwrite: bool = Query(False),
    user: dict = Depends(get_current_user),
):
    """上传 zip 技能包（可含多个 Skill 目录及其附属文件）。

    目录结构：zip 根目录放 SKILL.md，或一级/二级子目录各放一个 SKILL.md。
    同名 Skill 默认报错，overwrite=true 时覆盖。
    """
    data = await file.read()
    try:
        names = install_user_skill_bundle(
            user["id"], data, overwrite=overwrite, default_name=_upload_default_name(file.filename)
        )
    except BundleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _invalidate(user["id"])
    return {"ok": True, "installed": names}


@router.post("/skills/shared/import")
async def import_shared_skill(
    file: UploadFile = File(...),
    overwrite: bool = Query(False),
    user: dict = Depends(get_current_user),
):
    """把 zip 技能包装入公用技能目录（运维动作）。"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可导入公用 Skill")
    data = await file.read()
    try:
        names = install_shared_skill_bundle(
            data, overwrite=overwrite, default_name=_upload_default_name(file.filename)
        )
    except BundleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 公用技能对所有用户可见，逐用户失效太贵，这里只提示需要重启/下一轮生效。
    return {"ok": True, "installed": names}


@router.get("/me/skills", response_model=list[SkillOut])
async def list_mine(user: dict = Depends(get_current_user)):
    """当前用户可见的全部 Skill（内置 + 私有，私有同名覆盖内置）。"""
    return [_out(s) for s in list_skills(user["id"])]


@router.get("/me/skills/{name}")
async def get_skill_detail(name: str, user: dict = Depends(get_current_user)):
    """Return one skill's full body (for editing); only the owner can edit builtin."""
    from ..skill_store import get_skill
    spec = get_skill(user["id"], name)
    if spec is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return {
        "name": spec.name,
        "description": spec.description,
        "source": spec.source,
        "chars": spec.chars,
        "content": spec.body or "",
        "model_invocable": not spec.disable_model_invocation,
    }


@router.post("/me/skills", response_model=SkillOut)
async def create_skill(body: SkillIn, user: dict = Depends(get_current_user)):
    name, desc, content = _resolve_body(body)
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    try:
        spec = save_user_skill(user["id"], name, desc, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _invalidate(user["id"])
    return _out(spec)


@router.patch("/me/skills/{name}", response_model=SkillOut)
async def update_skill(name: str, body: SkillUpdate, user: dict = Depends(get_current_user)):
    try:
        builtin = is_builtin_skill(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if builtin and not (user_skill_exists(user["id"], name)):
        raise HTTPException(status_code=405, detail="内置/公用 Skill 只读，请先创建同名私有 Skill 覆盖")
    current = _find_own(user["id"], name)
    if current is None:
        raise HTTPException(status_code=404, detail="skill not found")
    new_name, desc, content = _resolve_body(body, current.body or "", current.description)
    target_name = new_name or name
    try:
        if target_name.lower() != name.lower():
            # 改名：先写新目录再删旧目录
            spec = save_user_skill(user["id"], target_name, desc, content)
            delete_user_skill(user["id"], name)
        else:
            spec = save_user_skill(user["id"], name, desc, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _invalidate(user["id"])
    return _out(spec)


@router.delete("/me/skills/{name}")
async def delete_skill(name: str, user: dict = Depends(get_current_user)):
    try:
        builtin = is_builtin_skill(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if builtin and not user_skill_exists(user["id"], name):
        raise HTTPException(status_code=405, detail="内置/公用 Skill 只读，无法删除")
    try:
        ok = delete_user_skill(user["id"], name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="skill not found")
    await _invalidate(user["id"])
    return {"ok": True}


def user_skill_exists(user_id: str, name: str) -> bool:
    from ..workspace import user_skills_root
    try:
        return (user_skills_root(user_id) / name.strip().lower() / "SKILL.md").is_file()
    except Exception:
        return False


def _find_own(user_id: str, name: str):
    from ..skill_store import get_skill
    spec = get_skill(user_id, name)
    return spec if spec is not None and spec.source == "user" else None
