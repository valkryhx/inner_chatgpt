"""FastAPI 入口：lifespan 装配 RuntimeHub + 路由 + CORS + 前端静态托管。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import ROOT_DIR, llm_yaml_available, load_llm_settings
from .routers import auth, chat, me, sessions, skills
from .runtime import RuntimeHub, set_hub
from .schemas import HealthOut


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    hub = RuntimeHub()
    await hub.startup()
    set_hub(hub)
    try:
        yield
    finally:
        await hub.shutdown()
        set_hub(None)


app = FastAPI(title="Multi-User Web Agent (self-built kernel)", version="0.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(me.router)
app.include_router(sessions.router)
app.include_router(chat.router)
app.include_router(skills.router)


@app.get("/api/health", response_model=HealthOut)
async def health():
    """健康检查：不依赖 llm.yaml（模型由每个用户自己配置）。

    llm.yaml 仅为本机可选兜底；缺失时返回 model="" 而不是 500，
    前端会显示当前会话/用户自己配置的模型名。
    """
    model = ""
    if llm_yaml_available():
        try:
            model = load_llm_settings().model
        except Exception:
            model = ""
    return HealthOut(ok=True, model=model, mcp_tools=0)


# ---- 前端静态托管 ----
_DIST = ROOT_DIR / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(_DIST / "index.html")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        candidate = _DIST / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
