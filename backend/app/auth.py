"""认证与授权：bcrypt 密码哈希 + JWT access/refresh + FastAPI 依赖。

对齐 Aivory：短时效 access token、长时效 refresh token（refresh 存 DB 可撤销），
鉴权时以 DB 的 status/token_ver 为准。
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Optional

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException

from . import db

SECRET = os.environ.get("APP_JWT_SECRET", "dev-secret-change-me-inner-chatgpt")
ALG = "HS256"
ACCESS_TTL = int(os.environ.get("APP_ACCESS_TTL_SECONDS", 60 * 60 * 24))       # 1 天
REFRESH_TTL = int(os.environ.get("APP_REFRESH_TTL_SECONDS", 60 * 60 * 24 * 30))  # 30 天


# ---------------- passwords ----------------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


# ---------------- tokens ----------------
def _make_token(user_id: str, token_ver: int, typ: str, ttl: int, jti: Optional[str] = None) -> tuple[str, dict]:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "tv": token_ver,
        "typ": typ,
        "iat": now,
        "exp": now + ttl,
    }
    if jti:
        payload["jti"] = jti
    token = jwt.encode(payload, SECRET, algorithm=ALG)
    return token, payload


def create_access_token(user_id: str, token_ver: int) -> tuple[str, int]:
    token, payload = _make_token(user_id, token_ver, "access", ACCESS_TTL)
    return token, payload["exp"]


def create_refresh_token(user_id: str, token_ver: int) -> tuple[str, str, int]:
    """返回 (token, jti, expires_at)。调用方需把 jti 落库。"""
    jti = uuid.uuid4().hex
    token, payload = _make_token(user_id, token_ver, "refresh", REFRESH_TTL, jti=jti)
    return token, jti, payload["exp"]


def decode_token(token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET, algorithms=[ALG])
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid_token: {e}")
    if payload.get("typ") != expected_type:
        raise HTTPException(status_code=401, detail="wrong_token_type")
    return payload


# ---------------- dependencies ----------------
def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


async def get_current_user(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    token = _extract_bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="auth_required")
    payload = decode_token(token, "access")
    user = await db.get_user(payload["sub"])
    if not user:
        raise HTTPException(status_code=401, detail="user_not_found")
    if user.get("status") != "active":
        raise HTTPException(status_code=403, detail="account_blocked")
    if int(user.get("token_ver", 0)) != int(payload.get("tv", -1)):
        raise HTTPException(status_code=401, detail="session_expired")
    return user
