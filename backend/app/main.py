"""FastAPI 入口：lifespan 装配 RuntimeHub + 路由 + CORS + 前端静态托管。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import ROOT_DIR, llm_yaml_available, load_llm_settings
from .logging_setup import setup_logging
from .routers import auth, chat, me, media, sessions, skills, team, workspace_files
from .routers import memory as memory_router
from .a2a import router as a2a_router
from .runtime import RuntimeHub, set_hub
from .schemas import HealthOut

# 必须早于其它模块的模块级 logger 调用：否则 handler 还没装上，
# 启动阶段的日志会静默丢失。
_LOG_CONFIG = setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging

    logging.getLogger(__name__).info(
        "logging ready: level=%s team_log=%s",
        _LOG_CONFIG["level"], _LOG_CONFIG["team_log"] or "(disabled)")
    await db.init_db()
    from .a2a import store as a2a_store
    await a2a_store.init_tables()
    # 子代理侧链表：幂等建表 + 把上次进程遗留的 running 收敛为 errored
    try:
        from .subagent import store as subagent_store
        await subagent_store.init_tables()
        await subagent_store.mark_stale_running_as_errored("server restart")
    except Exception:
        pass
    # GA 会话结果快照表：关闭后仍能补读（2026-11-11 事故后加）。
    try:
        from .acp import store as ga_store
        await ga_store.init_tables()
    except Exception:
        pass
    # Team facts + generation leases. Recover every active lease left by the
    # previous process before accepting requests, otherwise a single stale row
    # would block that session forever.
    from . import team_store
    await team_store.init_tables()
    await team_store.recover_stale_runtime("server restart")
    # 记忆索引表（幂等）。正文永远在 backend/data/memories/<user_id>/private/*.md，
    # 这里只建「列举 / 排序 / 命中统计」用的索引表。
    try:
        from . import memory_store
        await memory_store.init_tables()
    except Exception:
        pass
    # dream 状态表 + 启动收敛（清残留锁、running -> interrupted）。
    # **只收敛不补跑**：启动时正要连 MCP、建连接池，dream 会抢资源拖慢首帧。
    # 与 subagent_store / team_store 的 recover_* 是同一类问题、同一套解法。
    try:
        from . import memory_dream
        await memory_dream.init_tables()
        await memory_dream.recover_dream_state("server restart")
    except Exception:
        pass
    hub = RuntimeHub()
    await hub.startup()
    set_hub(hub)
    dream_handle = None
    try:
        # 记忆合并巡检任务（M4.5）。默认 600s 醒一次，内置 24h / 5 会话三道闸。
        from . import memory_dream
        if memory_dream.dream_enabled():
            dream_handle = memory_dream.start_dream_loop(
                memory_dream.default_client_provider)
        yield
    finally:
        if dream_handle is not None:
            await dream_handle.stop()
        await hub.shutdown()
        set_hub(None)


app = FastAPI(title="Hlaudz (self-built agent kernel)", version="0.3.0", lifespan=lifespan)

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
app.include_router(memory_router.router)
app.include_router(media.router)
app.include_router(workspace_files.router)
app.include_router(team.router)
app.include_router(a2a_router.router)
app.include_router(a2a_router.api_router)


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
