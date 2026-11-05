"""Skill 路由：用户私有 Skill 的 CRUD + 内置 Skill 列表（按 user_id 隔离）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_user
from ..schemas import SkillIn, SkillOut, SkillUpdate
from ..skill_store import (
    delete_user_skill,
    is_builtin_skill,
    list_builtin_skills,
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


def _out(spec) -> SkillOut:
    return SkillOut(
        name=spec.name,
        description=spec.description,
        source=spec.source,
        chars=spec.chars,
        updated_at=_updated_at(spec),
        model_invocable=not spec.disable_model_invocation,
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
        raise HTTPException(status_code=405, detail="内置 Skill 只读，请先创建同名私有 Skill 覆盖")
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
        raise HTTPException(status_code=405, detail="内置 Skill 只读，无法删除")
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
