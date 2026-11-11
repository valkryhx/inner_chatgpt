"""会话 CRUD 路由（多用户：全部依赖当前用户，DAO 按 user_id 隔离）。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from .. import db
from ..auth import get_current_user
from ..config import load_llm_settings
from ..history_store import get_history_store
from ..schemas import (
    ContextUsageOut,
    CreateSessionIn,
    MessageOut,
    MessageSearchOut,
    SessionDetailOut,
    SessionListOut,
    SessionOut,
    UpdateSessionIn,
)
from ..tokens import message_token_counter

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("/search", response_model=MessageSearchOut)
async def search_sessions(
    q: str = "",
    limit: int = 20,
    session_id: str = "",
    user: dict = Depends(get_current_user),
):
    """按正文关键词检索当前用户的历史对话。

    只搜 user / assistant 两类消息：system 消息是平台注入的，命中它会让用户
    搜到一段对话里根本看不见的文本。
    """
    started = time.perf_counter()
    limit = max(1, min(int(limit or 20), 100))
    hits, has_more = await db.search_messages(
        user["id"], q, limit=limit, session_id=session_id
    )
    return MessageSearchOut(
        query=q,
        hits=hits,
        has_more=has_more,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
    )


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
    title = (body.title or "").strip()[:200] or "新会话"
    sess = await db.create_session(user["id"], title=title, model_id=model_id, provider="")
    return SessionOut(**sess)


@router.get("/{session_id}", response_model=SessionDetailOut)
async def get_session(session_id: str, user: dict = Depends(get_current_user)):
    sess = await db.get_session(session_id, user["id"])
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    # 自愈：若该会话没有活跃生成，却存在遗留的 streaming 消息，则收敛为 stopped，
    # 避免前端把它一直显示为“正在思考”。
    from ..runtime import has_active_generation
    if not has_active_generation(session_id, user["id"]):
        # The process has no live task for this session.  Converge a lease
        # left behind after a client disconnect/crash before reporting history,
        # otherwise the next send would be rejected with 409 forever.
        from .. import team_store
        active_generation = await team_store.get_active_generation(user["id"], session_id)
        if active_generation:
            await team_store.converge_generation_facts(
                user["id"], session_id, active_generation, status="stale"
            )
        else:
            # No lease either, but a team may still be marked active if a
            # prior process failed between its two writes.  Converge any such
            # orphan so the TeamPanel cannot remain "running" forever.
            await team_store.converge_orphaned_active_teams(user["id"], session_id)
        await db.mark_stale_streaming_stopped(session_id)
    msgs = await db.list_messages(session_id)
    return SessionDetailOut(session=SessionOut(**sess), messages=[MessageOut(**m) for m in msgs])


async def _context_limits(user_id: str, model_id: str) -> tuple[int, int, int]:
    """Resolve the same soft/hard thresholds the agent loop will use.

    Reading the config is intentionally side-effect free; this endpoint must
    never build a runtime or connect to MCP just to show a token counter.
    """
    cfg = None
    if model_id:
        cfg = await db.get_llm_config(user_id, model_id)
    if not cfg:
        cfg = await db.get_default_llm_config(user_id)
    if cfg:
        ctx = int(cfg.get("context_window") or 128000)
        auto = int(cfg.get("auto_compact_tokens") or int(ctx * 0.90))
        hard = int(cfg.get("hard_limit_tokens") or int(ctx * 0.95))
        return ctx, auto, hard
    try:
        settings = load_llm_settings()
        return settings.context_window, settings.soft_limit_tokens, settings.hard_limit_tokens
    except Exception:
        ctx = 128000
        return ctx, int(ctx * 0.90), int(ctx * 0.95)


@router.get("/{session_id}/context", response_model=ContextUsageOut)
async def get_context_usage(session_id: str, user: dict = Depends(get_current_user)):
    sess = await db.get_session(session_id, user["id"])
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    history = await get_history_store().get(user["id"], session_id)
    used = message_token_counter(history)
    ctx, auto, hard = await _context_limits(user["id"], sess.get("model_id", "") or "")
    ctx = max(1, ctx)
    auto = max(1, auto)
    compaction = sess.get("compaction") or {}
    return ContextUsageOut(
        used_tokens=used,
        context_window=ctx,
        auto_compact_tokens=auto,
        hard_limit_tokens=hard,
        percent=round(used / ctx * 100, 2),
        compact_percent=round(used / auto * 100, 2),
        remaining_to_compact=max(0, auto - used),
        compacted=bool(compaction.get("compacted")),
    )


@router.patch("/{session_id}", response_model=SessionOut)
async def update_session(session_id: str, body: UpdateSessionIn, user: dict = Depends(get_current_user)):
    sess = await db.get_session(session_id, user["id"])
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "title" in fields:
        title = str(fields["title"] or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title 不能为空")
        fields["title"] = title[:200]
    if "reasoning_effort" in fields:
        from ..reasoning import InvalidReasoningEffort, normalize_effort
        try:
            # 允许显式传空串清空覆盖；非法值在这里就挡掉，不要留到调模型时才炸
            fields["reasoning_effort"] = normalize_effort(fields["reasoning_effort"])
        except InvalidReasoningEffort as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if "model_id" in fields:
        model_id = str(fields["model_id"] or "").strip()
        # An empty model_id means "clear the binding"; a non-empty one must be
        # a config the caller actually owns, otherwise the next send would
        # fail deep inside the runtime with an opaque error.
        if model_id and not await db.get_llm_config(user["id"], model_id):
            raise HTTPException(status_code=400, detail="model_id 不存在或不属于当前用户")
        fields["model_id"] = model_id
    updated = await db.update_session(session_id, user["id"], **fields)
    return SessionOut(**updated)


@router.delete("/{session_id}")
async def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    sess = await db.get_session(session_id, user["id"])
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    await db.delete_session(session_id, user["id"])
    from ..runtime import get_hub
    from .. import team_store
    hub = get_hub()
    if hub and hub.store is not None:
        try:
            await hub.store.delete(user["id"], session_id)
        except Exception:
            pass
    try:
        await team_store.delete_session_data(user["id"], session_id)
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
