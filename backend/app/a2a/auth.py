"""Bearer authentication and path/token/tenant binding for A2A requests."""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import HTTPException

from .. import db
from . import store, tokens


def _bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        value = parts[1].strip()
        return value or None
    return None


async def authenticate(authorization: Optional[str], agent_id: str,
                       tenant: Optional[str] = None) -> dict[str, Any]:
    raw = _bearer(authorization)
    if not raw:
        raise HTTPException(status_code=401, detail="a2a_auth_required")
    token = await store.find_token_by_hash(tokens.hash_token(raw))
    if not token:
        raise HTTPException(status_code=401, detail="a2a_invalid_token")
    if token.get("revoked_at"):
        raise HTTPException(status_code=401, detail="a2a_token_revoked")
    expires_at = token.get("expires_at")
    if expires_at and int(expires_at) <= int(time.time() * 1000):
        raise HTTPException(status_code=401, detail="a2a_token_expired")
    if token.get("agent_id") != agent_id:
        raise HTTPException(status_code=403, detail="a2a_agent_mismatch")
    if tenant and tenant != agent_id:
        raise HTTPException(status_code=403, detail="a2a_tenant_mismatch")
    user = await db.get_user(token["user_id"])
    if not user:
        raise HTTPException(status_code=403, detail="a2a_user_not_found")
    if user.get("status") != "active":
        raise HTTPException(status_code=403, detail="a2a_user_blocked")
    await store.touch_token(token["id"])
    return {
        "user_id": token["user_id"],
        "agent_id": agent_id,
        "user": user,
        "token": token,
        # 能力档位随身份一起下发：建 token 时定死，运行期不再询问任何人。
        "tool_policy": token.get("tool_policy") or "full",
    }
