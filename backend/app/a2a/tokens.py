"""Per-user opaque A2A bearer tokens. Only the SHA-256 hash is stored."""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any, Optional

from ..tool_policy import normalize as normalize_policy
from . import store

PREFIX = "hlz_a2a_"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return PREFIX + secrets.token_urlsafe(32)


async def create_token(user_id: str, name: str = "", scopes: Optional[list[str]] = None,
                       expires_in_days: Optional[int] = 90,
                       tool_policy: str = "full") -> tuple[str, dict[str, Any]]:
    # 未知档位在这里就报错，绝不把脏值写进库后靠运行期兜底。
    level = normalize_policy(tool_policy)
    agent = await store.ensure_agent(user_id)
    raw = generate_token()
    expires_at = None
    if expires_in_days and expires_in_days > 0:
        expires_at = int(time.time() * 1000) + int(expires_in_days) * 86_400_000
    info = await store.create_token(
        user_id=user_id,
        agent_id=agent["agent_id"],
        name=name.strip()[:80],
        token_hash=hash_token(raw),
        prefix=raw[:12],
        scopes=scopes or ["chat"],
        expires_at=expires_at,
        tool_policy=level,
    )
    return raw, info
