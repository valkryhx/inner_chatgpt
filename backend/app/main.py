"""FastAPI 入口：lifespan 装配 RuntimeHub + 路由 + CORS + 前端静态托管。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
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

    _log = logging.getLogger(__name__)
    # 启动横幅：多实例下排查的第一性问题就是「这个容器是谁」。
    # 一条日志同时给出 owner、注入标识、日志文件位置，省掉 exec 进去看环境变量。
    _identity = _instance_identity()
    _log.info(
        "startup instance=%s app_log=%s level=%s team_log=%s",
        _identity or "(unknown)",
        _LOG_CONFIG.get("app_log") or "(disabled)",
        _LOG_CONFIG["level"],
        _LOG_CONFIG["team_log"] or "(disabled)")
    # PG 后端先预热连接池：冷建连 ~200ms，首个请求不该承担它
    # （记忆预取有 50ms 级的时序要求，见 pgdriver.warm_pool）。
    try:
        from . import dbselect, pgdriver
        if dbselect.is_postgres():
            await pgdriver.warm_pool()
    except Exception:  # noqa: BLE001 - 预热失败不该阻止启动
        pass
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
    # 多实例：只收敛**已死实例**遗留的 lease。把本集群存活实例的 owner
    # 传进去，正在别的实例上跑着的 generation/task 不能被清掉 ——
    # 否则 A 还在写、B 已判定它死了，状态分叉。
    # 没配 Redis 时 live_owners() 返回空集合 => 全清（单实例语义不变）。
    try:
        from .cluster import instances as _inst
        _live = await _inst.live_owners()
    except Exception:  # noqa: BLE001
        _live = set()
    await team_store.recover_stale_runtime("server restart", live_owners=_live)
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
    # 多实例协调：登记本实例 + 起心跳。**必须在沙箱启动之前**——
    # 沙箱启动清扫会查「本机有哪些活着的实例」，本实例不先登记进去，
    # 就会被自己（或别的实例）当成死实例清掉刚建的容器。
    #
    # 没配 HLZ_REDIS_URL 时这些调用全是空操作，单实例行为不变。
    instance_hb = None
    try:
        from .cluster import instances as inst_registry
        await inst_registry.register_self()
        instance_hb = asyncio.create_task(_instance_heartbeat_loop())
        # 登记完立刻打一条集群快照：多实例排查时「启动这一刻有几个兄弟」
        # 是最关键的信息（后面谁死了、谁新加了都要跟这一条对比）。
        _snap = await _cluster_snapshot()
        _log.info(
            "cluster enabled=%s live=%d leader=%s instance_id=%s owner=%s",
            _snap.enabled, _snap.live_instances, _snap.leader,
            _snap.instance_id or "(none)", _identity or "(unknown)")
    except Exception:
        _log.warning("实例登记失败（按单实例继续）", exc_info=True)
    # 沙箱管理器：自检 Docker/镜像 + 清扫上次进程残留容器 + 起 TTL 回收协程。
    # 自检失败**不阻断启动**（auto 模式降级留痕、strict 模式在工具调用点报错），
    # 这与设计文档 §6.5「沙箱故障不能拖垮主服务」一致。
    try:
        from .sandbox.manager import get_manager as get_sandbox_manager
        sbx_ok, sbx_reason = await get_sandbox_manager().start()
        if not sbx_ok:
            logging.getLogger(__name__).warning("沙箱自检未通过：%s", sbx_reason)
    except Exception:
        logging.getLogger(__name__).warning("沙箱管理器启动失败", exc_info=True)
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
        # 心跳先停：否则退出过程中还在往 Redis 写，等于宣告「我活着」
        if instance_hb is not None:
            instance_hb.cancel()
            try:
                await instance_hb
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # 注销要在**容器销毁之后**：否则别的实例会在这一瞬间
        # 把本实例还活着的容器当残留清掉。顺序见下面沙箱 shutdown。
        # Turn jobs 先收：它们跑着生成，必须在 runtime 被拆掉之前停下来，
        # 否则工具/子代理会在一个已被 close 的 runtime 上继续跑。
        # 取消是**优雅**的：`_event_stream` 的 finally 会把消息收敛成 stopped。
        try:
            from . import turn_job
            await turn_job.shutdown_all("server shutdown")
        except Exception:
            pass
        await hub.shutdown()
        set_hub(None)
        # 沙箱放最后：前面的 turn_job / hub 收尾期间可能还有工具在跑，
        # 容器必须先活着。这里销毁全部容器并停掉 TTL 回收协程。
        try:
            from .sandbox.manager import get_manager as get_sandbox_manager
            n = await get_sandbox_manager().shutdown()
            if n:
                logging.getLogger(__name__).info("已销毁 %d 个沙箱容器", n)
        except Exception:
            logging.getLogger(__name__).warning("沙箱管理器关闭失败", exc_info=True)
        # 容器都销毁了，才把自己从存活实例表里移除
        try:
            from .cluster import instances as inst_registry
            await inst_registry.unregister_self()
        except Exception:
            pass


async def _instance_heartbeat_loop() -> None:
    """定期刷新本实例的心跳。

    没有 Redis 时 `heartbeat()` 直接返回，所以这个协程是空转的（很快）。
    单独写成模块级函数而不是塞进 lifespan，是为了让测试能直接调它。
    """
    try:
        from .cluster import instances as inst_registry
    except Exception:  # noqa: BLE001
        return
    while True:
        try:
            await asyncio.sleep(30)
            await inst_registry.heartbeat()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - 心跳失败不该杀掉协程
            logging.getLogger(__name__).debug("实例心跳异常", exc_info=True)


app = FastAPI(title="Hlaudz (self-built agent kernel)", version="0.3.0", lifespan=lifespan)


@app.middleware("http")
async def _instance_header(request: Request, call_next):
    """给每个响应打上 `X-HLZ-Instance`，标明是哪个实例处理的。

    ## 为什么需要

    多实例部署下，「这个请求落到哪个实例」是排查的第一性问题：

    - 验证 nginx 粘性是否真的生效（同一 cookie 是否总命中同一实例）
    - 用户报「改了配置没生效」时，先看两次请求是不是落到了不同实例
      （runtime / MCP 连接缓存是进程内的，不共享）
    - 定位「为什么这个实例的容器特别多」

    没有这个头，只能去翻各实例的日志，且要按时间戳人工对齐。

    ## 为什么用 header 而不是日志

    日志需要登录服务器；header 前端 / curl / 浏览器 DevTools 直接能看到。

    ## 安全

    只暴露 `<host>:<pid>`，不含任何用户信息或路径。
    生产环境如果不想暴露，用 `HLZ_INSTANCE_HEADER=0` 关掉。
    """
    import os
    response = await call_next(request)
    # 粘性 cookie：**任何**响应都下发，不只 health（2026-11-19 审计 §2）。
    #
    # 原实现只在 `/api/health` 里调（全仓唯一调用点），而没访问过 health 的
    # 客户端永远拿不到 cookie。而 `hash $cookie_hlz_sticky` 在**没有 cookie 时
    # 不是随机打散，是所有无 cookie 请求落到同一个上游** —— 那批用户集中压在
    # 单个实例上，续看时查不到进程内的 job。
    #
    # 真实触发场景：清空站点数据 / curl 直接打 API / 首屏 health 失败
    # （App.tsx 里是 catch(() => null) 静默跳过）/ 扩缩容后 nginx 重启。
    try:
        _ensure_sticky_cookie(request, response)
    except Exception:  # noqa: BLE001 - 下发失败不该影响响应
        pass
    if (os.environ.get("HLZ_INSTANCE_HEADER", "1") or "1").strip() not in ("0", "false", "False"):
        try:
            from .sandbox.identity import owner_id
            response.headers["X-HLZ-Instance"] = owner_id()
        except Exception:  # noqa: BLE001 - 拿不到就不打，不该影响响应
            pass
    return response

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
async def health(request: Request, response: Response):
    """健康检查：不依赖 llm.yaml（模型由每个用户自己配置）。

    llm.yaml 仅为本机可选兜底；缺失时返回 model="" 而不是 500，
    前端会显示当前会话/用户自己配置的模型名。

    ## 顺手下发粘性 cookie

    nginx 的 `hash $cookie_hlz_sticky`（**不带 consistent**，见 nginx.conf 的说明）
    **只能读** cookie，没法设。所以由应用下发一个稳定的随机值，让同一个浏览器
    始终落到同一个后端实例。

    ⚠️ 2026-11-19 起，cookie 由 `_instance_header` 中间件在所有响应上下发，
    **不再只在这里**（原本这里是全仓唯一调用点，导致没访问过 health 的客户端
    永远没有粘性）。见 `docs/20261119-多实例正确性审计.md` §2。

    ## 多实例可观测性（2026-11-19 加）

    返回 `instance` 与 `cluster`，让运维**不进容器**就能回答：
    这次请求落在谁身上、集群里还有谁活着、我是不是 leader。
    全部可安全公开 —— 不含 Redis URL / DB DSN / 密码。
    """
    model = ""
    if llm_yaml_available():
        try:
            model = load_llm_settings().model
        except Exception:
            model = ""
    return HealthOut(ok=True, model=model, mcp_tools=0,
                     instance=_instance_identity(),
                     cluster=await _cluster_snapshot())


def _instance_identity() -> str:
    """本实例 owner（`<hostname>:<标识>`）。取不到返回空串，绝不抛。"""
    try:
        from .sandbox.identity import owner_id
        return owner_id()
    except Exception:  # noqa: BLE001
        return ""


async def _cluster_snapshot():
    """集群形态快照。任何失败都降级成「单实例」，不影响 health 可用性。

    **只返回形状**：有没有配 Redis、活着几个实例、我是不是 leader、我的标识。
    绝不回传连接串。
    """
    from .schemas import ClusterHealthOut
    try:
        from .cluster import instances as inst
        from .cluster.config import cluster_enabled, instance_id
        enabled = cluster_enabled()
        live = 0
        if enabled:
            try:
                live = len(await inst.live_owners())
            except Exception:  # noqa: BLE001
                live = 0
        is_leader = False
        election = "off"
        try:
            from .cluster import get_elector
            elector = get_elector()
            if elector.is_running:
                is_leader = bool(elector.is_leader)
                election = "on"
            elif enabled:
                # 配了 Redis 但选举循环没跑：`leader` 值不可信。
                # 如实报 false + 状态，而不是让运维以为高可用正常。
                #
                # 2026-11-19：`LeaderElector` 目前**没有任何生产消费者**
                # （`start()` 从未被调用，`on_become_leader` 从未注册；
                # 启动清扫用的是 sandbox.manager 自己的独立租约）。
                # 所以这里报 "disabled" 而不是 "starting" —— 后者会让人以为
                # 「选举马上就好」，实际含义是「永远不会」。
                # 详见 docs/20261119-多实例正确性审计.md §11.1。
                is_leader = False
                election = "disabled"
            else:
                # 单实例：本来就是唯一实例
                is_leader = True
                election = "off"
        except Exception:  # noqa: BLE001
            is_leader = not enabled
            election = "off"
        return ClusterHealthOut(enabled=enabled, live_instances=live,
                                leader=is_leader, instance_id=instance_id(),
                                leader_election=election)
    except Exception:  # noqa: BLE001
        return ClusterHealthOut()


# 粘性 cookie 名。与 deploy/nginx.conf 的 `hash $cookie_hlz_sticky` 必须一致。
STICKY_COOKIE = "hlz_sticky"


def _ensure_sticky_cookie(request, response) -> None:
    """没有粘性 cookie 就发一个（随机值，不是用户标识）。"""
    try:
        if request.cookies.get(STICKY_COOKIE):
            return
        import secrets
        value = secrets.token_hex(16)
        response.set_cookie(
            STICKY_COOKIE, value,
            max_age=30 * 24 * 3600,      # 30 天
            path="/",
            httponly=True,               # 前端不需要读它
            samesite="lax",
            # secure：只在 HTTPS 下开。HTTP 部署时设 True 会让浏览器
            # 直接丢弃这个 cookie，粘性静默失效 —— 比没有更糟。
            secure=False,
        )
    except Exception:  # noqa: BLE001 - 下发失败不该让健康检查失败
        pass


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
