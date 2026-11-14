"""记忆路由：设置页「我的记忆」tab 的 CRUD（按 user_id 隔离）。

与 ``routers/skills.py`` 同构，但只做记忆自己需要的四件事：
列举（带分页）、读单条、写、删。

两处刻意与工具面不同：

1. **这里**有 DELETE，工具面没有。CC 客户端只承诺
   ``memory_list`` / ``memory_read`` / ``memory_write`` 三件（记忆方案 §5），
   对外工具契约不该多承诺；但「用户想删掉一条记错的记忆」是必须有的产品能力，
   所以它只出现在给**界面**用的 REST 路由里。
2. **写入强制带 if_version 才做 CAS**，不带就按普通覆盖处理。
   界面上用户是唯一编辑者，弹冲突会让「我就想改一下」变得别扭；
   模型侧工具则鼓励带 if_version（后台子代理与用户手改可能并发）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import memory_store as ms
from ..auth import get_current_user

router = APIRouter(prefix="/api", tags=["memory"])


class MemoryIn(BaseModel):
    slug: str = Field(default="", description="留空则从 title 派生")
    title: str = ""
    description: str = ""
    content: str = ""
    kind: str = "user"
    tags: list[str] = Field(default_factory=list)
    if_version: str | None = None


class MemoryPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    content: str | None = None
    kind: str | None = None
    tags: list[str] | None = None
    if_version: str | None = None


def _out(spec: ms.MemorySpec) -> dict:
    return spec.to_public_dict()


@router.get("/me/memories")
async def list_memories(
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    query: str = Query(""),
    user: dict = Depends(get_current_user),
):
    """当前用户的记忆（按 slug 升序分页，``cursor`` 续读）。"""
    try:
        page = await ms.list_memories(user["id"], limit=limit, cursor=cursor, query=query)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"读取记忆失败: {e}") from e
    payload = page.to_dict()
    # 设置页要「最近更新优先」，而存储层按 slug 排序（cursor 才稳定）。
    # 在**当前页内**按 updated_at 降序即可满足直觉：翻页语义仍由 cursor 保证。
    payload["entries"].sort(key=lambda e: (-int(e.get("updated_at") or 0), e["slug"]))
    return payload


@router.get("/me/memories/status")
async def memory_status(user: dict = Depends(get_current_user)):
    """功能开关 + 条数快照（设置页展示「当前 12 条，自动写入已关闭」）。"""
    try:
        total = await ms.count_memories(user["id"])
    except Exception:  # noqa: BLE001
        total = 0
    return {
        "enabled": ms.memory_enabled(),
        "auto_write": ms.auto_write_enabled(),
        "dream_enabled": ms.dream_enabled(),
        "disabled_by_env": ms.memory_hard_disabled(),
        "total": total,
        "inject_budget": ms.inject_budget(),
        "inject_max": ms.inject_max_entries(),
    }


@router.get("/me/memories/{slug}")
async def get_memory(slug: str, user: dict = Depends(get_current_user)):
    spec = await ms.get_memory(user["id"], slug)
    if spec is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {**_out(spec), "content": spec.body or ""}


@router.post("/me/memories")
async def create_memory(body: MemoryIn, user: dict = Depends(get_current_user)):
    slug = (body.slug or "").strip() or ms.slugify(body.title or body.description)
    res = await ms.write_memory(
        user["id"], slug=slug, title=body.title, description=body.description,
        body=body.content, kind=body.kind, tags=list(body.tags),
        if_version=body.if_version,
    )
    if res.outcome == "conflict":
        raise HTTPException(status_code=409, detail={
            "message": res.message,
            "current_version": res.current_version,
            "current_content": res.current_content,
        })
    if not res.ok or res.spec is None:
        raise HTTPException(status_code=400, detail=res.message or res.outcome)
    return _out(res.spec)


@router.patch("/me/memories/{slug}")
async def update_memory(slug: str, body: MemoryPatch, user: dict = Depends(get_current_user)):
    current = await ms.get_memory(user["id"], slug)
    if current is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    # 「没给」与「给了空串」必须分开：前者是「别动这个字段」，后者是「清空它」。
    title = current.title if body.title is None else body.title
    desc = current.description if body.description is None else body.description
    kind = current.kind if body.kind is None else body.kind
    tags = list(current.tags) if body.tags is None else list(body.tags)
    content = current.body if body.content is None else body.content
    res = await ms.write_memory(
        user["id"], slug=slug, title=title, description=desc, body=content,
        kind=kind, tags=tags, if_version=body.if_version,
        # 设置页的语义是「用户看到什么就是什么」：显式清空必须真的清空。
        # 工具的语义相反（留空=沿用），两者不能共用一个默认值。
        merge=False,
    )
    if res.outcome == "conflict":
        raise HTTPException(status_code=409, detail={
            "message": res.message,
            "current_version": res.current_version,
            "current_content": res.current_content,
        })
    if not res.ok or res.spec is None:
        raise HTTPException(status_code=400, detail=res.message or res.outcome)
    return _out(res.spec)


@router.delete("/me/memories/{slug}")
async def delete_memory(slug: str, user: dict = Depends(get_current_user)):
    ok = await ms.delete_memory(user["id"], slug)
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True}
