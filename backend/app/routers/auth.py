"""认证路由：注册 / 登录 / 登出 / 当前用户 / 刷新。"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .. import db
from ..auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)
from ..schemas import AuthResponse, LoginIn, RegisterIn, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        path="/api/auth",
    )


async def _issue(response: Response, user: dict) -> AuthResponse:
    access, exp = create_access_token(user["id"], int(user.get("token_ver", 0)))
    refresh, jti, refresh_exp = create_refresh_token(user["id"], int(user.get("token_ver", 0)))
    await db.add_refresh_token(jti, user["id"], refresh_exp)
    _set_refresh_cookie(response, refresh)
    return AuthResponse(
        user=UserOut(id=user["id"], email=user["email"], username=user.get("username", ""),
                     name=user.get("name", ""), role=user.get("role", "user")),
        access_token=access,
        expires_at=exp,
    )


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")


@router.post("/register", response_model=AuthResponse)
async def register(body: RegisterIn, response: Response):
    email = (body.email or "").lower().strip()
    username = (body.username or "").strip().lower()
    if "@" not in email or len(body.password) < 4:
        raise HTTPException(status_code=400, detail="邮箱格式不正确，或密码少于 4 位")
    if username and not USERNAME_RE.match(username):
        raise HTTPException(
            status_code=400,
            detail="用户名只能包含字母、数字、下划线、点或短横线，长度 2-32 位",
        )
    # 重复校验：邮箱与用户名分别提示，前端直接展示 detail。
    if await db.email_exists(email):
        raise HTTPException(status_code=400, detail="邮箱已被注册，请直接登录或换一个邮箱")
    if username and await db.username_exists(username):
        raise HTTPException(status_code=400, detail="用户名已被占用，请换一个用户名")
    try:
        user = await db.create_user(
            email, hash_password(body.password), name=body.name or "", username=username
        )
    except Exception:
        # 并发注册时由唯一索引兜底，仍给出可读提示。
        raise HTTPException(status_code=400, detail="用户名或邮箱已被注册")
    return await _issue(response, user)


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginIn, response: Response):
    ident = (body.identifier or body.username or body.email or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="请输入邮箱或用户名")
    row = await db.get_user_with_hash_by_identifier(ident)
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="邮箱/用户名或密码错误")
    if row.get("status") != "active":
        raise HTTPException(status_code=403, detail="账号已被停用")
    return await _issue(response, row)


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        try:
            payload = decode_token(token, "refresh")
            await db.revoke_refresh_token(payload.get("jti", ""))
        except HTTPException:
            pass
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: dict = Depends(get_current_user)):
    return UserOut(id=user["id"], email=user["email"], username=user.get("username", ""),
                   name=user.get("name", ""), role=user.get("role", "user"))


@router.post("/refresh", response_model=AuthResponse)
async def refresh(request: Request, response: Response):
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="no_refresh_token")
    payload = decode_token(token, "refresh")
    jti = payload.get("jti", "")
    row = await db.get_refresh_token(jti)
    if not row or row.get("revoked"):
        raise HTTPException(status_code=401, detail="refresh_revoked")
    user = await db.get_user(payload["sub"])
    if not user or user.get("status") != "active":
        raise HTTPException(status_code=401, detail="user_unavailable")
    # 旋转：撤销旧 refresh，签发新的
    await db.revoke_refresh_token(jti)
    return await _issue(response, user)
