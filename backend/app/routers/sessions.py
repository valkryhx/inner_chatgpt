"""会话 CRUD 路由（多用户：全部依赖当前用户，DAO 按 user_id 隔离）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import db
from ..auth import get_current_user
from ..schemas import (
    CreateSessionIn,
    MessageOut,
    SessionDetailOut,
    SessionListOut,
    SessionOut,
    UpdateSessionIn,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("", response_model=SessionListOut)
async def list_sessions(limit: int = 50, offset: int = 0, user: dict = Depends(get_current_user)):
    sessions, has_more = await db.list_sessions(user["id"], limit=limit, offset=offset)
    return SessionListOut(sessions=[SessionOut(**s) for s in sessions], has_more=has_more)


@router.post("", response_model=SessionOut)
async def create_session(body: CreateSessionIn, user: dict = Depends(get_current_user)):
    model_id = body.model_id or ""
    if not model_id:
        # 新会话默认使用用户“默认”的模型配置；没有默认则退回第一条可用配置
        cfg = await db.get_default_llm_config(user["id"])
        if not cfg:
            for c in await db.list_llm_configs(user["id"]):
                if c.get("api_key") and c.get("model") and c.get("base_url"):
                    cfg = c
                    break
        if cfg and cfg.get("api_key") and cfg.get("model") and cfg.get("base_url"):
            model_id = cfg["id"]
    sess = await db.create_session(user["id"], title=body.title or "新会话", model_id=model_id, provider="")
    return SessionOut(**sess)


@router.get("/{session_id}", response_model=SessionDetailOut)
async def get_session(session_id: str, user: dict = Depends(get_current_user)):
    sess = await db.get_session(session_id, user["id"])
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    # 自愈：若该会话没有活跃生成，却存在遗留的 streaming 消息，则收敛为 stopped，
    # 避免前端把它一直显示为“正在思考”。
    from ..runtime import has_active_generation
    if not has_active_generation(session_id):
        await db.mark_stale_streaming_stopped(session_id)
    msgs = await db.list_messages(session_id)
    return SessionDetailOut(session=SessionOut(**sess), messages=[MessageOut(**m) for m in msgs])


@router.patch("/{session_id}", response_model=SessionOut)
async def update_session(session_id: str, body: UpdateSessionIn, user: dict = Depends(get_current_user)):
    sess = await db.get_session(session_id, user["id"])
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    updated = await db.update_session(session_id, user["id"], **fields)
    return SessionOut(**updated)


@router.delete("/{session_id}")
async def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    sess = await db.get_session(session_id, user["id"])
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    await db.delete_session(session_id, user["id"])
    from ..runtime import get_hub
    hub = get_hub()
    if hub and hub.store is not None:
        try:
            await hub.store.delete(session_id)
        except Exception:
            pass
    return {"ok": True}


@router.get("/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(session_id: str, user: dict = Depends(get_current_user)):
    sess = await db.get_session(session_id, user["id"])
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    msgs = await db.list_messages(session_id)
    return [MessageOut(**m) for m in msgs]
