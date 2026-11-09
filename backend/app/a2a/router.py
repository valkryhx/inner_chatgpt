"""A2A JSON-RPC endpoint, per-user AgentCard and token-management API."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..auth import get_current_user
from . import auth, cards, config, service, store, tokens
from .errors import A2AError, internal_error, invalid_params, task_not_found

router = APIRouter(tags=["a2a"])
api_router = APIRouter(prefix="/api/a2a", tags=["a2a"])


def _base_url(request: Request) -> str:
    return config.public_base_url() or str(request.base_url).rstrip("/")


def _require_enabled() -> None:
    if not config.enabled():
        raise HTTPException(status_code=404, detail="a2a_disabled")


@router.get("/.well-known/agent-card.json")
async def global_agent_card():
    # Deliberately no global callable agent. Each user has a per-user card URL.
    raise HTTPException(status_code=404, detail="use_per_user_agent_card")


@router.get("/a2a/{agent_id}/.well-known/agent-card.json")
async def agent_card(agent_id: str, request: Request):
    _require_enabled()
    agent = await store.get_agent_by_agent_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent_not_found")
    return cards.build_agent_card(agent_id, _base_url(request))


@router.get("/a2a/{agent_id}")
async def agent_card_at_base(agent_id: str, request: Request):
    # Convenience for clients that treat the interface URL itself as the card
    # reference. The canonical location remains the well-known path above.
    return await agent_card(agent_id, request)


@router.post("/a2a/{agent_id}")
async def jsonrpc(agent_id: str, request: Request):
    _require_enabled()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "parse error"}})
    if not isinstance(body, dict):
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32600, "message": "invalid request"}})
    req_id = body.get("id")
    if body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
        return JSONResponse({"jsonrpc": "2.0", "id": req_id,
                             "error": {"code": -32600, "message": "invalid request"}})
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return JSONResponse({"jsonrpc": "2.0", "id": req_id,
                             "error": {"code": -32602, "message": "params must be an object"}})
    identity = await auth.authenticate(
        request.headers.get("authorization"), agent_id, params.get("tenant")
    )
    method = body["method"]
    if method in ("SendStreamingMessage", "SubscribeToTask"):
        try:
            if method == "SendStreamingMessage":
                row = await service.prepare_stream(identity, params)
            else:
                task_id = str(params.get("id") or "")
                if not task_id:
                    raise invalid_params("id is required")
                row = await store.get_task(identity["user_id"], agent_id, task_id)
                if row is None:
                    raise task_not_found(task_id)
                if row["state"] in service._TERMINAL:
                    raise A2AError(-32004, "task is not streamable",
                                   data={"taskId": task_id})
        except A2AError as exc:
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": exc.to_jsonrpc()},
                                status_code=exc.http_status)
        return StreamingResponse(
            service.stream_row(row, req_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )
    try:
        result = await service.handle_method(identity, method, params)
        if method == "SendMessage":
            result = {"task": result}
    except A2AError as exc:
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": exc.to_jsonrpc()},
                            status_code=exc.http_status)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"jsonrpc": "2.0", "id": req_id,
                             "error": internal_error(f"{type(exc).__name__}: {exc}").to_jsonrpc()})
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


class TokenIn(BaseModel):
    name: str = ""
    scopes: list[str] = Field(default_factory=lambda: ["chat"])
    expires_in_days: Optional[int] = 90


@api_router.get("/agent")
async def my_agent(request: Request, user: dict = Depends(get_current_user)):
    _require_enabled()
    agent = await store.ensure_agent(user["id"])
    base = _base_url(request)
    return {
        "agent_id": agent["agent_id"],
        "url": f"{base}/a2a/{agent['agent_id']}",
        "card_url": f"{base}/a2a/{agent['agent_id']}/.well-known/agent-card.json",
        "tenant": agent["agent_id"],
    }


@api_router.get("/tokens")
async def my_tokens(user: dict = Depends(get_current_user)):
    _require_enabled()
    rows = await store.list_tokens(user["id"])
    for row in rows:
        row.pop("token_hash", None)
    return {"tokens": rows}


@api_router.post("/tokens")
async def create_token(body: TokenIn, request: Request, user: dict = Depends(get_current_user)):
    _require_enabled()
    unsupported = sorted(set(body.scopes or []) - {"chat"})
    if unsupported:
        raise HTTPException(status_code=400, detail=f"unsupported_scope: {', '.join(unsupported)}")
    raw, info = await tokens.create_token(
        user["id"], name=body.name, scopes=body.scopes,
        expires_in_days=body.expires_in_days,
    )
    info = dict(info)
    info.pop("token_hash", None)
    base = _base_url(request)
    return {
        **info,
        "token": raw,
        "url": f"{base}/a2a/{info['agent_id']}",
        "card_url": f"{base}/a2a/{info['agent_id']}/.well-known/agent-card.json",
        "warning": "copy this token now; it is shown only once",
    }


@api_router.post("/tokens/{token_id}/revoke")
async def revoke_token(token_id: str, user: dict = Depends(get_current_user)):
    _require_enabled()
    if not await store.revoke_token(user["id"], token_id):
        raise HTTPException(status_code=404, detail="token_not_found")
    return {"ok": True}
