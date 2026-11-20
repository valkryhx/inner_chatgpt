"""沙箱服务的 HTTP 门面。

## 边界

这一层**只做四件事**：认证、参数校验、错误映射、调 executor。
执行逻辑一行都不在这里——那在 `executor/` 后面（方案 §8.2）。

## 为什么 user_id 必须来自认证

方案的底线（§4.4 约束 1、§2.5.1 的 A.7-2）：
**用户身份与执行边界不能由模型/调用方决定**。
`code_run` 的参数是模型生成的，如果 `user_id` 能被请求体指定，
模型一句 `{"user_id": "u_someone_else"}` 就能跨租户读文件。

所以：认证身份是唯一来源；请求体里若带了且不一致，**403 而不是静默忽略**
（静默忽略会让人以为身份被接受了，排查时找不到线索）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .archive import VolBackend, pack_dir, unpack_dir
from .executor import ExecResult, get_executor
from .pool import SlotBusy, SlotPool
from .workspace import (
    PathSecurityError,
    ensure_dir_shared,
    make_resolver,
    resolve_in_workspace,
    workspaces_root,
)

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """把 app 自己的 logger 接到 stdout。

    ## 为什么必须显式配

    uvicorn 只配置它自己的 logger（`uvicorn.*`），**不会**给
    `sandbox_service.*` 配 handler。于是 `logger.info("建槽 ...")` 全部被丢弃——
    端到端探针就因此拿不到「池有没有真的建槽」的证据（本次实际踩到）。

    这类"日志看起来在工作其实没输出"的问题在排查时特别耗时，
    因为服务行为是对的、只是看不到。
    """
    root = logging.getLogger("sandbox_service")
    if root.handlers:      # 避免重复加 handler（reload / 多测例）
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(os.environ.get("HLZ_SANDBOX_LOG_LEVEL", "INFO").upper())


_configure_logging()

# ---- 常量（照 Aivory 的护栏做法，见方案 §3.0）----

MAX_CODE_BYTES = 1024 * 1024          # 1 MiB
MAX_CONTENT_BYTES = 4 * 1024 * 1024   # 4 MiB（写文件；比 code 宽，正文可能较大）
MAX_TIMEOUT_S = 600.0                 # 10 分钟上限
DEFAULT_TIMEOUT_S = 120.0
MIN_TIMEOUT_S = 1.0
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024  # 200 MiB（与 Aivory 的默认一致）

# ---- 进程内单例（lifespan 里建，测试可替换）----

_pool: SlotPool | None = None
_archive: VolBackend | None = None
_executor = None      # 预热起的 daemon 挂在它身上，**必须单例**（见 _get_executor）


def _get_executor():
    """进程内唯一的执行器实例。

    ## 为什么必须单例（这是一次真实的假验收）

    `executor.get_executor()` 是**工厂**，每次调用都新建实例。而预热的 daemon
    存在实例的 `_daemons` 字典里，于是：

        建槽 → get_executor() 返回实例 A → start() 在 A 上起了 daemon
        exec → get_executor() 返回实例 B → B._daemons 是空的 → 走一次性模式

    池的日志会显示「槽预热完成」，性能收益却是零——比"没接预热"更难发现，
    因为每一处单看都是对的。上一轮实测第二次调用 567ms（= 重新 import pandas）
    才暴露出来。

    ## 为什么这里叫 `get_executor` 的模块级名字仍然保留

    下面的实现调用**模块全局**的 `get_executor`，所以测试里
    `monkeypatch.setattr(svc_main, "get_executor", ...)` 依然能替换掉真实构造。
    """
    global _executor
    if _executor is None:
        _executor = get_executor()
    return _executor


def _reset_executor() -> None:
    """清空单例（**仅供测试**跨测例隔离；生产路径不调用）。"""
    global _executor
    _executor = None


def _get_pool() -> SlotPool:
    global _pool
    if _pool is None:
        _pool = SlotPool(
            max_slots=int(os.environ.get("HLZ_SANDBOX_MAX_SLOTS", "3")),
            workspace_resolver=make_resolver(),
            overflow=os.environ.get("HLZ_SANDBOX_OVERFLOW", "queue"),
            queue_timeout_s=float(os.environ.get("HLZ_SANDBOX_QUEUE_TIMEOUT_S", "60")),
            idle_ttl_s=float(os.environ.get("HLZ_SANDBOX_IDLE_TTL_S", "300")),
            preload=os.environ.get("HLZ_SANDBOX_PRELOAD", "numpy,pandas,matplotlib"),
            # ★ 预热回调：建槽时真的起常驻解释器（不是只记账）
            prewarm=_prewarm_user,
            # ★ 销毁回调：槽被销毁/淘汰/空闲回收时停掉 daemon
            on_destroy=_stop_user_daemon,
        )
    return _pool


async def _prewarm_user(user_id: str) -> object:
    """建槽时给它起一个常驻解释器（方案 §4.2 D）。

    ## 返回值的三种情况（调用方按「只有 False 算失败」判定）

    - `True`：daemon 起来了，后续 exec 走复用（8.8ms 级）
    - `False`：起不来（没装 bwrap / 资源不足），exec 退化到一次性模式（~600ms）
    - `None`：执行器不需要预热（inproc），正常状态

    ## 为什么失败不抛

    把预热失败做成硬错误会让「容器里没装 bwrap」直接导致服务不可用——
    功能没坏，只是慢了 600ms，不该升级成故障。
    """
    try:
        return await _get_executor().start(user_id)
    except Exception:  # noqa: BLE001
        logger.warning("预热用户失败 user=%s", user_id, exc_info=True)
        return False


async def _stop_user_daemon(user_id: str) -> None:
    """停掉某个用户的常驻解释器（槽被回收时调）。"""
    try:
        await _get_executor().destroy(user_id)
    except Exception:  # noqa: BLE001
        logger.warning("停 daemon 失败 user=%s", user_id, exc_info=True)


def _get_archive() -> VolBackend:
    global _archive
    if _archive is None:
        _archive = VolBackend()
    return _archive


def _archive_slot(slot) -> None:
    """淘汰 / 回收槽前先归档（池通过 on_evict 回调调它）。

    方案 §4.7.4 铁律 2：淘汰前必须归档，否则丢用户文件。
    """
    from .archive import archive_key

    try:
        if not slot.workspace.exists():
            return
        data = pack_dir(slot.workspace, max_bytes=MAX_ARCHIVE_BYTES)
        key = archive_key(slot.user_id)
        _get_archive().put(key, data)
        logger.info("归档槽 user=%s key=%s bytes=%d", slot.user_id, key, len(data))
    except Exception:  # noqa: BLE001
        logger.warning("归档失败 user=%s（继续回收）", slot.user_id, exc_info=True)


async def _maybe_restore(slot) -> None:
    """槽刚建时尝试从归档恢复 workspace（方案 §7.5）。

    ## 为什么只在「槽刚建」时恢复

    槽的生命周期内 workspace 是活的文件系统，反复解包会把用户的
    新改动覆盖掉。所以只在**新建槽**（= 之前被回收/淘汰过）时恢复一次。

    ## ⚠️ 「一次」必须真的只做一次

    docstring 一直写着「只恢复一次」，但代码里曾经**没有守卫**——
    `exec_code` 每次调用都无条件走到这里，于是每执行一次代码就解包一遍。
    这个 bug 被巧合掩盖了：归档只在 `_evict` 时写，内容恰好等于磁盘内容，
    「解包回去」和「不动它」结果一样。

    只要归档比磁盘旧就会静默回滚用户数据。实测（容器内）：

        disk: CURRENT
        （放入旧归档）
        after 2nd exec: STALE     ← 用户写的 CURRENT 没了

    归档比磁盘旧的真实场景：服务写归档时崩溃（部分写入）、
    将来接了对象存储从远端拉回旧版本、或者人工恢复了一份旧备份。

    ## 守卫挂在 Slot 上（`archived_restored`），不是挂在用户上

    槽被回收后再次 acquire 会新建一个 Slot 对象，那时**应该**恢复一次
    （这期间磁盘目录可能被别的东西动过，恢复是它的职责）。
    标记挂在用户上会让新槽永远不恢复——方向相反的同类问题。

    ## 缺归档 / 归档坏了

    「没有归档」是正常状态（第一次用），不是错误。
    归档坏了也不该让用户跑不了代码——记日志、当作没有归档。
    """
    if getattr(slot, "archived_restored", False):
        return
    # 先置位再解包：解包中途失败也不该在下次重试（重试只会再失败一遍，
    # 而且可能把已经写进去的一部分文件和归档叠在一起）。
    slot.archived_restored = True

    from .archive import archive_key

    key = archive_key(slot.user_id)
    data = _get_archive().get(key)
    if not data:
        return
    try:
        unpack_dir(data, slot.workspace)
        logger.info("恢复归档 user=%s key=%s bytes=%d",
                    slot.user_id, key, len(data))
    except Exception:  # noqa: BLE001
        logger.warning("恢复归档失败 user=%s key=%s", slot.user_id, key, exc_info=True)


async def _reap_loop(pool: SlotPool, interval_s: float = 60.0) -> None:
    """空闲槽回收协程（lifespan 起，关闭时取消）。

    ## 为什么必须有这个循环

    `pool.reap_idle()` 本身写得再对，**没人调就等于没有**。上一轮就是这样：
    `IDLE_TTL_S=300` 配了、函数也实现了，但服务里没有任何地方调它 ——
    于是槽只会越建越多，到 `max_slots` 就永远 `queue` 等下去（因为
    「槽被 TTL 回收腾出名额」是 queue 分支唯一的出路之一，见 pool.py 注释）。

    这是池的**另一半**：建 + 收。少了收，池就退化成"只涨不落"。

    ## 为什么 60 秒一次

    TTL 默认 300s，60s 的粒度意味着实际回收发生在 300~360s 之间，
    对"省内存"这个目的完全够用，又不会频繁扫表。间隔写死 60s 不暴露成
    环境变量：调这个值除了浪费 token 不会带来收益，真正的旋钮是 TTL。
    """
    while True:
        try:
            await asyncio.sleep(interval_s)
            n = await pool.reap_idle()
            if n:
                logger.info("空闲回收 %d 个槽（现有 %d/%d）",
                            n, pool.size(), pool.max_slots)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # 回收循环自己不能死：一次异常让循环退出，槽就再也不会被回收
            logger.warning("空闲回收循环出错（继续）", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建池（**不预建槽**，方案 §4.7.6：预建的空槽绑不上用户，白占内存）。"""
    pool = _get_pool()
    pool._on_evict = _archive_slot    # 接上归档回调
    ok, reason = await _get_executor().health()
    if not ok:
        logger.error("执行器自检失败：%s", reason)

    # ★ 起空闲回收协程：池的「收」这一半
    reap_task = asyncio.create_task(_reap_loop(pool))

    logger.info("沙箱服务启动：max_slots=%d overflow=%s idle_ttl_s=%.0f",
                pool.max_slots, pool.overflow, pool.idle_ttl_s)
    yield

    # 先停回收循环（避免它与关闭归档并发操作同一批槽）
    reap_task.cancel()
    try:
        await reap_task
    except asyncio.CancelledError:
        pass

    # 关闭时归档所有槽，避免丢工作区
    for slot in list(pool._slots.values()):
        _archive_slot(slot)
    logger.info("沙箱服务关闭")


app = FastAPI(title="Hlaudz Sandbox Service", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------- 认证


def _expected_token() -> str:
    """服务间共享 token。空 = 未配置。"""
    return (os.environ.get("HLZ_SANDBOX_SERVICE_TOKEN") or "").strip()


def authenticate(
    authorization: Annotated[str | None, Header()] = None,
    x_hlz_user: Annotated[str | None, Header()] = None,
) -> str:
    """校验 token 并取出调用方声明的用户身份。

    ⚠️ 这里的 `X-HLZ-User` 是 **app 服务端**填的（app 已经认证过用户），
    不是模型能控制的——`builtin_tools` 里 `user_id` 来自请求上下文，
    不来自工具参数。方案 §4.4 的接口草案就是这么设计的。

    Returns:
        已认证的 user_id。

    Raises:
        401: token 缺失或不匹配
        401: 缺 X-HLZ-User
    """
    expected = _expected_token()
    # 未配置 token 时**拒绝启动式失败**（照 Aivory 的 SANDBOX_API_KEY 做法）
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务未配置 HLZ_SANDBOX_SERVICE_TOKEN，拒绝执行",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 Authorization: Bearer <token>",
        )
    got = authorization.split(" ", 1)[1].strip()
    if got != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="token 不匹配"
        )
    if not x_hlz_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 X-HLZ-User（用户身份必须由 app 服务端声明）",
        )
    return x_hlz_user


# ---------------------------------------------------------------- 模型


class ExecBody(BaseModel):
    code: str
    code_type: str = "python"
    timeout_s: float = DEFAULT_TIMEOUT_S
    cwd: str = ""
    # 出现在请求体里只用于**检测冲突**，绝不作为身份来源（见模块 docstring）
    user_id: str | None = None


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool


class WriteBody(BaseModel):
    """写文件请求。

    ⚠️ `path` 是**相对 workspace 根**的路径，不是容器内的 `/ws/...`，
    也不是宿主绝对路径。理由见 `workspace.resolve_in_workspace`。
    """

    path: str
    content: str
    mode: str = "overwrite"      # overwrite | append | prepend
    # 同 ExecBody：只用于检测冲突，绝不作为身份来源
    user_id: str | None = None


class WriteResponse(BaseModel):
    path: str
    chars: int
    mode: str


class PatchBody(BaseModel):
    path: str
    old: str
    new: str
    user_id: str | None = None


# ---------------------------------------------------------------- 路由


@app.get("/v1/health")
async def health() -> dict:
    """健康检查。**不要求认证**（容器 healthcheck 用）。"""
    ex = _get_executor()
    ok, reason = await ex.health()
    return {"ok": ok, "executor": type(ex).__name__, "reason": reason}


@app.post("/v1/exec", response_model=ExecResponse)
async def exec_code(
    body: ExecBody,
    user_id: Annotated[str, Depends(authenticate)],
) -> ExecResponse:
    """执行代码。

    错误映射（调用方据此决定要不要重试）：
    - 400 路径越界 / 参数不合法
    - 401 认证失败
    - 403 请求体身份与认证身份冲突
    - 413 code 太大
    - 422 参数校验失败
    - 504 执行超时
    """
    # --- 身份冲突：不静默忽略 ---
    if body.user_id is not None and body.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"请求体 user_id={body.user_id!r} 与认证身份不符。"
                "用户身份只能由 app 服务端通过 X-HLZ-User 声明，不能由请求体指定。"
            ),
        )

    # --- 参数校验 ---
    code = body.code or ""
    if not code.strip():
        raise HTTPException(status_code=422, detail="code 不能为空")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        # 用字面量 413 而不是 status.HTTP_413_REQUEST_ENTITY_TOO_LARGE：
        # 后者在新版 starlette 里已弃用（改名 CONTENT_TOO_LARGE），
        # 而旧版本没有新常量 —— 字面量两边都能用。
        raise HTTPException(
            status_code=413,
            detail=f"code 超过 {MAX_CODE_BYTES} 字节",
        )
    if body.code_type not in ("python", "sh"):
        raise HTTPException(
            status_code=422, detail=f"不支持的 code_type: {body.code_type!r}"
        )
    if body.timeout_s <= 0:
        raise HTTPException(status_code=422, detail="timeout_s 必须为正")

    timeout_s = max(MIN_TIMEOUT_S, min(body.timeout_s, MAX_TIMEOUT_S))

    # --- 拿槽（扩缩容 + overflow 都在这里）---
    pool = _get_pool()
    try:
        slot = await pool.acquire(user_id)
    except SlotBusy as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": exc.reason,
                "queue_wait_ms": exc.queue_wait_ms,
            },
        ) from exc

    # --- 首访恢复归档（稳定 key，方案 §7.5）---
    await _maybe_restore(slot)

    # --- 执行 ---
    # ★ 必须用单例：预热的 daemon 就在这个实例上，换实例 = 复不上（走一次性）
    ex = _get_executor()
    try:
        result: ExecResult = await ex.exec(
            user_id, code, code_type=body.code_type, timeout_s=timeout_s, cwd=body.cwd
        )
    except PathSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await pool.release(user_id)

    if result.exit_code == 124:
        # 504 而不是 200：调用方需要知道这不是「代码跑完了但返回非零」
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "message": f"执行超过 {timeout_s}s 被终止",
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )

    return ExecResponse(
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=result.duration_ms,
        truncated=result.truncated,
    )

# ---------------------------------------------------------------- 文件写


def _conflict_guard(body_user_id: str | None, user_id: str) -> None:
    """请求体里的 user_id 与认证身份冲突 → 403。

    与 `exec_code` 同一条规则：身份只能来自认证，请求体里带的只用来
    **检测冲突**（静默忽略会让人以为身份被接受了，排查时找不到线索）。
    """
    if body_user_id is not None and body_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"请求体 user_id={body_user_id!r} 与认证身份不符。"
                "用户身份只能由 app 服务端通过 X-HLZ-User 声明。"
            ),
        )


@app.post("/v1/write", response_model=WriteResponse)
async def write_file(
    body: WriteBody,
    user_id: Annotated[str, Depends(authenticate)],
) -> WriteResponse:
    """把内容写进该用户 workspace（服务侧）。

    ## 为什么服务侧要自己做路径校验

    app 已经校验过一次（`safe_workspace_path`），但这里**仍要校验**：
    调用方可能被攻破，也可能有别的入口直连。只信调用方等于开放任意写。
    见 `workspace.resolve_in_workspace` 的 docstring。

    ## 错误映射

    - 400 路径越界 / 非法参数
    - 401 认证失败
    - 403 身份冲突
    - 413 内容过大
    """
    _conflict_guard(body.user_id, user_id)

    mode = (body.mode or "overwrite").strip().lower()
    if mode not in ("overwrite", "append", "prepend"):
        raise HTTPException(
            status_code=422,
            detail=f"不支持的 mode: {mode!r}（overwrite/append/prepend）",
        )

    content = body.content if isinstance(body.content, str) else str(body.content)
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise HTTPException(
            status_code=413, detail=f"content 超过 {MAX_CONTENT_BYTES} 字节"
        )

    try:
        target = resolve_in_workspace(user_id, body.path)
    except PathSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        # 用 ensure_dir_shared 而不是裸 mkdir：模型会写到
        # .subagents/<s>/<a>/<自拟子目录>，那几层都是这里建的，
        # 裸 mkdir 给 0755（组不可写），app 侧下次就写不进去。
        ensure_dir_shared(target.parent, boundary=workspaces_root(user_id))
        existing = ""
        if mode != "overwrite" and target.exists():
            existing = target.read_text(encoding="utf-8", errors="replace")
        if mode == "append":
            new_text = existing + content
        elif mode == "prepend":
            new_text = content + existing
        else:
            new_text = content

        # 先写临时文件再 rename：避免部分写入被当成完整文件
        tmp = target.with_name(target.name + ".hlz-tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        # 磁盘满 / 权限 / 路径被占：是服务端问题，不是参数问题
        logger.warning("写入失败 user=%s path=%s", user_id, body.path, exc_info=True)
        raise HTTPException(
            status_code=507, detail=f"写入失败：{exc.__class__.__name__}: {exc}"
        ) from exc

    rel = body.path.strip().lstrip("/")
    logger.info("文件写入 user=%s path=%s chars=%d mode=%s",
                user_id, rel, len(new_text), mode)
    return WriteResponse(path=rel, chars=len(new_text), mode=mode)


@app.post("/v1/patch", response_model=WriteResponse)
async def patch_file(
    body: PatchBody,
    user_id: Annotated[str, Depends(authenticate)],
) -> WriteResponse:
    """把文件里的第一处 `old` 替换成 `new`。

    语义与 app 侧的 `file_patch` 一致：**只替换第一处**，找不到就 409
    （不是静默成功 —— 那会让模型以为改好了）。
    """
    _conflict_guard(body.user_id, user_id)

    if not body.old:
        raise HTTPException(status_code=422, detail="old 不能为空")

    try:
        target = resolve_in_workspace(user_id, body.path)
    except PathSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {body.path}")

    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=507, detail=f"读取失败：{exc}") from exc

    if body.old not in text:
        raise HTTPException(
            status_code=409,
            detail="要替换的内容不存在（file_patch 只替换第一处，找不到即失败）",
        )

    updated = text.replace(body.old, body.new, 1)
    try:
        tmp = target.with_name(target.name + ".hlz-tmp")
        tmp.write_text(updated, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise HTTPException(status_code=507, detail=f"写入失败：{exc}") from exc

    rel = body.path.strip().lstrip("/")
    logger.info("文件修改 user=%s path=%s chars=%d", user_id, rel, len(updated))
    return WriteResponse(path=rel, chars=len(updated), mode="patch")
