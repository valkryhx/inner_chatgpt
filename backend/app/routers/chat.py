"""聊天路由（多用户）：SSE 流式生成、停止、手动压缩、MCP 清单。"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import db
from ..agent import with_effort
from ..auth import get_current_user
from ..runtime import (
    get_hub, register_generation, request_stop, unregister_generation,
    reserve_generation, release_generation_reservation, cancel_generation,
    is_generation_live,
)
from ..process_registry import stop_generation as stop_code_runs
from .. import team_store
from ..schemas import CompactResult, MessageOut, SendMessageIn, StopIn, StopResult
from ..sse import run_agent_stream, sse_frame
from .. import uploads
from ..subagent.context import AgentContext
from ..subagent.session import build_turn_runtime, prepare_turn_team

_SUBAGENT_EVENT_ID = "__subagent__"


def _register_acp_manager(hub, user_id: str, session_id: str, generation_id: str,
                          turn) -> None:
    """Publish the turn's ACP manager so ``/stop`` can reach it.

    Without this the graceful ``hub.stop_acp_generation`` is a no-op in
    production and the only remaining kill path is the turn's own cleanup.
    """
    register = getattr(hub, "register_acp_manager", None)
    acp = getattr(turn, "acp", None)
    if not callable(register) or acp is None:
        return
    try:
        register(user_id, session_id, generation_id, acp)
    except BaseException:
        logger.warning("register ACP manager failed", exc_info=True)


async def _merged_events(main_gen, buffer, host, stop_event=None, extra=None):
    """Merge main and child events; wait briefly for live children at main EOF.

    A root model can legitimately finish its response immediately after
    spawning a teammate.  The old implementation closed the child buffer at
    that instant, so a reviewer could be marked stopped after 0 turns while it
    was still starting.  Keep the SSE alive while child tasks are active.
    Users can still stop through the normal stop path.
    """
    q: asyncio.Queue = asyncio.Queue()

    def _active(obj) -> bool:
        fn = getattr(obj, "has_active", None)
        try:
            return bool(fn()) if callable(fn) else False
        except Exception:
            return False

    def has_active() -> bool:
        # ACP sessions (``extra``) live outside the subagent host but belong to
        # the same turn, so the stream must stay open while either is running.
        return _active(host) or _active(extra)

    def drain_timeout() -> float:
        try:
            return max(0.5, float(os.environ.get("TEAM_DRAIN_TIMEOUT_S", "300")))
        except (TypeError, ValueError):
            return 300.0

    async def pump_main():
        try:
            async for ev in main_gen:
                q.put_nowait(ev)
        except Exception as e:  # noqa: BLE001
            q.put_nowait({"type": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            q.put_nowait({"type": _SUBAGENT_EVENT_ID})

    async def pump_sub():
        try:
            async for ev in buffer.stream():
                q.put_nowait(ev)
        except Exception:
            pass

    t_main = asyncio.create_task(pump_main())
    t_sub = asyncio.create_task(pump_sub())
    main_done = False
    deadline = 0.0
    try:
        while True:
            timeout = None
            if main_done:
                if stop_event is not None and stop_event.is_set():
                    break
                if not has_active():
                    break
                if not deadline:
                    deadline = asyncio.get_running_loop().time() + drain_timeout()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    for target in (host, extra):
                        stop_all = getattr(target, "stop_all", None)
                        if not callable(stop_all):
                            continue
                        try:
                            await stop_all()
                        except Exception:
                            logger.warning("drain-timeout stop failed", exc_info=True)
                    break
                timeout = min(0.25, remaining)
            try:
                ev = await asyncio.wait_for(q.get(), timeout=timeout)
            except asyncio.TimeoutError:
                continue
            if ev.get("type") == _SUBAGENT_EVENT_ID:
                main_done = True
                continue
            yield ev

        # Drain events already queued by an agent that became terminal between
        # the last queue read and the has_active() check.
        try:
            while True:
                ev = await asyncio.wait_for(q.get(), timeout=0.2)
                if ev.get("type") != _SUBAGENT_EVENT_ID:
                    yield ev
        except asyncio.TimeoutError:
            pass
    finally:
        buffer.close()
        for task in (t_main, t_sub):
            if not task.done():
                task.cancel()
        for task in (t_main, t_sub):
            try:
                await task
            except BaseException:
                pass


class _TurnUR:
    """让 run_agent_stream() 能跑在「本轮派生 loop」上的最小 shim。"""

    def __init__(self, turn):
        self.loop = turn.loop
        self.store = turn.store
        self.model = turn.model
        self.user_id = getattr(turn, "user_id", "")

logger = logging.getLogger(__name__)

# Cancellation-critical bookkeeping is moved to fresh tasks: once a streaming
# task is cancelled, every *new* await in it raises immediately, so awaiting
# the terminal DB write or the generation-lease release there would drop it.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    """Run ``coro`` outside the caller's cancel scope and keep it referenced."""
    try:
        task = asyncio.create_task(coro)
    except BaseException:
        logger.warning("schedule background task failed", exc_info=True)
        return
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _finalize_and_persist_after_cancel(
    finalize: Callable[[str], None],
    persist: Callable[[str], Awaitable[None]],
    status: str,
) -> None:
    """Finalize in-memory blocks now, persist them from a fresh task."""
    finalize(status)
    _spawn_background(persist(status))


async def _release_or_background(release: Callable[[], Awaitable[None]]) -> None:
    """Await a lease release, retrying it in a fresh task if cancelled."""
    try:
        await release()
    except asyncio.CancelledError:
        _spawn_background(release())
        raise


async def _settle_turn(turn, user_id: str, session_id: str, generation_id: str,
                       status: str, hub=None) -> None:
    """Background fallback used when the request task was cancelled mid-cleanup.

    A fresh task is outside the anyio cancel scope, so these awaits can finish.
    """
    acp_close = getattr(getattr(turn, "acp", None), "close_all", None)
    if callable(acp_close):
        try:
            await acp_close()
        except BaseException:
            pass
    try:
        await turn.host.close_all()
    except BaseException:
        pass
    if turn.team is not None:
        try:
            await turn.team.set_status(status)
        except BaseException:
            pass
    try:
        await team_store.release_generation(user_id, session_id, generation_id)
    except BaseException:
        pass
    # Backup: ``_cleanup_turn`` already pops this synchronously, but a turn that
    # never reached it must not leave the manager registered forever.
    unregister_acp = getattr(hub, "unregister_acp_manager", None)
    if callable(unregister_acp):
        try:
            unregister_acp(user_id, session_id, generation_id)
        except BaseException:
            pass


async def _cleanup_turn(turn, hub, user_id: str, session_id: str, generation_id: str,
                        team_id: str, status: str) -> None:
    """Release a turn's resources without letting cancellation skip cleanup.

    Order matters: the synchronous releases run before any ``await`` so a
    cancelled anyio scope cannot interrupt them.  Every later await is guarded
    with ``BaseException`` (``CancelledError`` is not an ``Exception``);
    if one is interrupted we schedule a fresh task to finish the rest.
    """
    # 1) Cancellation-proof, synchronous releases.
    release_pending = None
    if turn.team is not None:
        release_now = getattr(turn.team, "release_lifecycle_now", None)
        if callable(release_now):
            try:
                release_now()
            except BaseException:
                logger.warning("release team lifecycle lease failed", exc_info=True)
        else:  # legacy/test doubles without the sync release
            release_pending = getattr(turn.team, "release_lifecycle", None)
    stop_now = getattr(turn.host, "stop_all_now", None)
    try:
        if callable(stop_now):
            stop_now()
    except BaseException:
        logger.warning("stop subagent tasks failed", exc_info=True)
    acp = getattr(turn, "acp", None)
    acp_stop_now = getattr(acp, "stop_all_now", None)
    try:
        if callable(acp_stop_now):
            acp_stop_now()
    except BaseException:
        logger.warning("stop ACP sessions failed", exc_info=True)
    # Drop the hub registration here rather than after an await: the async
    # section below can be interrupted (host.close_all is its own await), and a
    # registration left behind would leak one hub entry per stopped turn.
    unregister_acp = getattr(hub, "unregister_acp_manager", None)
    if callable(unregister_acp):
        try:
            unregister_acp(user_id, session_id, generation_id)
        except BaseException:
            logger.warning("unregister ACP manager failed", exc_info=True)
    try:
        hub.unregister_subagent_host(user_id, session_id, generation_id, team_id)
    except TypeError:
        try:
            hub.unregister_subagent_host(session_id, generation_id)
        except BaseException:
            logger.warning("unregister subagent host failed", exc_info=True)
    except BaseException:
        logger.warning("unregister subagent host failed", exc_info=True)

    # 2) Async cleanup; each await is independent.  On any interruption the
    # remaining bookkeeping moves to a fresh task outside the cancel scope, so
    # team_end is emitted once rather than once here and once again there.
    def _schedule_settle() -> None:
        try:
            asyncio.create_task(
                _settle_turn(turn, user_id, session_id, generation_id, status, hub))
        except BaseException:
            logger.warning("schedule turn settle failed", exc_info=True)

    if callable(release_pending):
        try:
            await release_pending()
        except BaseException:
            logger.warning("release team lifecycle lease was interrupted", exc_info=True)
    try:
        await turn.host.close_all()
    except asyncio.CancelledError:
        logger.info("close subagent host cancelled; settling in background")
        _schedule_settle()
        return
    except BaseException:
        logger.warning("close subagent host failed", exc_info=True)
        _schedule_settle()
        return
    acp_interrupted = False
    acp_close = getattr(acp, "close_all", None)
    if callable(acp_close):
        try:
            await acp_close()
        except asyncio.CancelledError:
            logger.info("close ACP sessions cancelled; settling in background")
            acp_interrupted = True
        except BaseException:
            logger.warning("close ACP sessions failed", exc_info=True)
            acp_interrupted = True
    if acp_interrupted:
        _schedule_settle()
        return
    if turn.team is not None:
        try:
            await turn.team.events_since(turn.team.observed_seq)
            await turn.team.set_status(status)
        except BaseException:
            logger.warning("team cleanup was interrupted", exc_info=True)
            _schedule_settle()

router = APIRouter(prefix="/api", tags=["chat"])


async def _event_stream(user_id: str, session_id: str, text: str, generation_id: str, message_id: str,
                        model_id: str = "", stop_event: asyncio.Event | None = None,
                        tool_policy: str = "full",
                        attachments: list[dict] | None = None,
                        reasoning_effort: str = "") -> AsyncIterator[str]:
    # Register/capture the current streaming task at the very top.  The
    # prelude below can await runtime construction and compaction; registering
    # only after it would leave stop unable to cancel a request that is stuck
    # before the first model call.
    if stop_event is None:
        stop_event = register_generation(user_id, session_id, generation_id)

    # Stop arrived during the pre-stream hand-off.  Still persist both visible
    # rows so history is consistent, but do not build a runtime or call an LLM.
    if stop_event.is_set():
        try:
            await db.add_message(session_id, "user", content=text,
                                 blocks=_attachment_blocks(attachments))
            await db.add_message(session_id, "assistant", content="", status="stopped",
                                 message_id=message_id)
        except Exception:
            pass
        event_id = 1
        yield sse_frame("message_start", {"message_id": message_id}, event_id)
        yield sse_frame("done", {"message_id": message_id, "status": "stopped", "model": ""}, event_id + 1)
        unregister_generation(user_id, session_id, generation_id)
        try:
            await team_store.release_generation(user_id, session_id, generation_id)
        except Exception:
            pass
        return

    # Persist the visible turn before the prelude (runtime build / compaction /
    # sanitize).  Those awaits can take seconds; if the user stops during them,
    # the stop endpoint must at least find the user row and assistant
    # placeholder to converge, instead of erasing what they just typed.
    try:
        await db.add_message(session_id, "user", content=text,
                             blocks=_attachment_blocks(attachments))
        await db.add_message(session_id, "assistant", content="", status="streaming", message_id=message_id)
    except Exception:
        pass

    hub = get_hub()
    if hub is None:
        await team_store.release_generation(user_id, session_id, generation_id)
        yield sse_frame("error", {"error": "runtime not ready"})
        return
    try:
        ur = await hub.get_user_runtime(user_id, model_id)
        # 会话级思考等级：派生视图，不改缓存里的 runtime（见 agent.with_effort）
        ur = with_effort(ur, reasoning_effort)
    except Exception as e:  # noqa: BLE001
        # 典型场景：全新部署、用户还没配置任何模型（且仓库不携带 llm.yaml）。
        # 这里以 SSE 事件返回可读提示，而不是让前端只看到 500。
        await team_store.release_generation(user_id, session_id, generation_id)
        yield sse_frame("error", {
            "error": f"无法初始化模型：{type(e).__name__}: {e}",
            "hint": "请先在「设置 -> 我的模型」新增模型（base_url / api_key / model）并设为默认。",
        })
        return

    # 本轮实际生效的思考等级：会话覆盖优先，否则用模型配置里的值。
    # 有些网关（如内部中转）会**静默忽略**不认识的 reasoning_effort，
    # 所以这里把「发出去的到底是什么」回显给前端，用户才能发现「选了但没生效」。
    try:
        eff_configured = (ur.model.client.s.reasoning_effort or "") if ur.model else ""
        eff_effective = reasoning_effort or eff_configured
        if eff_effective:
            yield sse_frame("reasoning_effort", {
                "effective": eff_effective,
                "source": "session" if reasoning_effort else "model_config",
                "model": ur.model.model_name if ur.model else "",
            })
    except Exception:
        pass

    # 软线自动压缩（对齐 GenericAgent）：本轮前按 token 阈值判断，超软线才摘要较早消息。
    # 与硬线不同，这里会真正调用 LLM 生成摘要，并替换 checkpoint 为「摘要 + 近段」。
    try:
        from ..compaction import enforce_soft_limit
        soft = await enforce_soft_limit(user_id, session_id, pending_text=text)
        if soft is not None:
            yield sse_frame("compaction", soft)
    except Exception:
        pass

    # 硬线兜底：本轮前若 checkpoint 已超硬线，先裁剪最旧消息（不调用 LLM），避免请求超窗。
    try:
        from ..compaction import enforce_hard_limit
        trimmed = await enforce_hard_limit(user_id, session_id)
        if trimmed is not None:
            yield sse_frame("compaction", {"trigger": "hard_trim", **trimmed})
    except Exception:
        pass

    # 自愈：若上一轮因工具异常中断，checkpoint 可能残留「AI 已发 tool_calls 但无 tool 结果」
    # 的悬空状态，会导致本次请求被模型服务 400 拒绝并级联污染会话。这里先修复（补结果或截断）。
    try:
        healed = await ur.sanitize_pending_tool_calls(session_id)
        if healed:
            yield sse_frame("healed", {"repaired": True})
    except Exception:
        pass

    buffer: list[str] = []
    reasoning_buf: list[str] = []
    tool_steps: list[dict[str, Any]] = []
    # 消息块按「事件真实发生顺序」累积：文本增量并入当前文本块，
    # 工具/图表在出现的位置插入。这样刷新后顺序与流式时一致。
    blocks: list[dict[str, Any]] = []
    event_id = 0
    persist_lock = asyncio.Lock()
    last_persist_at = 0.0
    terminal_status: str | None = None

    def _append_text(chunk: str) -> None:
        if blocks and blocks[-1].get("type") == "text":
            blocks[-1]["text"] += chunk
        else:
            blocks.append({"type": "text", "text": chunk})

    def _append_reasoning(chunk: str) -> None:
        # Keep thinking in true event order (reasoning -> tool -> reasoning -> text),
        # instead of appending the whole reasoning blob at the end.
        if blocks and blocks[-1].get("type") == "reasoning":
            blocks[-1]["text"] += chunk
        else:
            blocks.append({"type": "reasoning", "text": chunk})

    def frame(etype: str, data: dict[str, Any]) -> str:
        nonlocal event_id
        event_id += 1
        return sse_frame(etype, data, event_id)

    async def _persist(status: str, *, force: bool = False) -> None:
        nonlocal last_persist_at
        now = asyncio.get_running_loop().time()
        if not force and now - last_persist_at < 0.25:
            return
        async with persist_lock:
            last_persist_at = now
            content = "".join(buffer)
            reasoning = "".join(reasoning_buf)
            snapshot = [dict(b) for b in blocks]
            try:
                await db.update_message(message_id, content=content, reasoning=reasoning,
                                        blocks=snapshot, status=status)
            except Exception:
                pass

    yield frame("message_start", {"message_id": message_id})

    # ---- 子代理：本轮派生的 Runtime（含 5 个编排工具）+ 子代理事件合并进同一条 SSE ----
    ctx = AgentContext(user_id=user_id, session_id=session_id,
                       message_id=message_id, generation_id=generation_id)
    turn = None
    team_ctx = ctx
    try:
        turn = await build_turn_runtime(ur, ctx, tool_policy=tool_policy)
        # build_turn_runtime creates the server-owned team id. All host and
        # child contexts must use that same id; never trust a tool argument.
        if getattr(turn, "team_id", ""):
            team_ctx = ctx.with_team(turn.team_id, role="lead")
        try:
            hub.register_subagent_host(user_id, session_id, generation_id, team_ctx.team_id, turn.host)
        except TypeError:
            # Compatibility for lightweight test doubles with the pre-scope
            # signature. Production RuntimeHub always uses the five-argument form.
            hub.register_subagent_host(session_id, generation_id, turn.host)
        await prepare_turn_team(turn, team_ctx)
        # Register only once the turn is fully built: a failure above drops the
        # turn, and the stream's finally would then never clean it up.
        _register_acp_manager(hub, user_id, session_id, generation_id, turn)
    except Exception:
        turn = None

    _sub_blocks: dict[str, dict] = {}

    def _sub_block(agent_id, name="", role=""):
        b = _sub_blocks.get(agent_id)
        if b is None:
            b = {"type": "subagent", "agent_id": agent_id, "name": name or agent_id,
                 "role": role or "general", "status": "running", "task": "",
                 "events": [], "result": "", "turns": 0}
            _sub_blocks[agent_id] = b
            blocks.append(b)
        return b

    def _finalize_sub_blocks(status: str) -> None:
        """Mark still-running child cards as stopped when the turn ends abnormally.

        A stop/cancel tears the parent SSE down before the child's own
        ``subagent_end`` can be consumed, so without this the persisted block
        keeps ``status="running"`` and every refresh shows a card that can
        never finish.
        """
        if status not in ("stopped", "error"):
            return
        for b in _sub_blocks.values():
            if b.get("status") in ("running", "starting"):
                b["status"] = "stopped"

    try:
        if turn is not None:
            turn_ur = _TurnUR(turn)
            if not getattr(turn_ur, "user_id", ""):
                turn_ur.user_id = user_id
            events = _merged_events(run_agent_stream(turn_ur, session_id, text, stop_event,
                                                     attachments=attachments),
                                    turn.buffer, turn.host, stop_event,
                                    extra=getattr(turn, "acp", None))
        else:
            events = run_agent_stream(ur, session_id, text, stop_event, attachments=attachments)

        async for ev in events:
            et = ev["type"]
            if et == "text_delta":
                buffer.append(ev["text"])
                _append_text(ev["text"])
                await _persist("streaming")
                yield frame("text_delta", {"text": ev["text"]})
            elif et == "thinking_delta":
                reasoning_buf.append(ev["text"])
                _append_reasoning(ev["text"])
                await _persist("streaming")
                yield frame("thinking_delta", {"text": ev["text"]})
            elif et == "tool_start":
                step = {"type": "tool", "id": ev.get("id"), "name": ev.get("name"),
                        "input": ev.get("input")}
                tool_steps.append(step)
                blocks.append(step)
                await _persist("streaming")
                yield frame("tool_start", {"id": ev.get("id"), "name": ev.get("name"),
                                           "input": ev.get("input")})
            elif et == "chart":
                blocks.append({"type": "chart", "spec": ev["spec"]})
                await _persist("streaming")
                yield frame("chart", {"spec": ev["spec"]})
            elif et == "tool_result":
                for st in tool_steps:
                    if st["id"] == ev.get("id"):
                        st["result"] = ev.get("content")
                await _persist("streaming")
                yield frame("tool_result", {"id": ev.get("id"), "name": ev.get("name"),
                                            "content": ev.get("content"),
                                            "is_error": ev.get("is_error", False)})
            elif et == "subagent_start":
                b = _sub_block(ev.get("agent_id", ""), ev.get("name", ""), ev.get("role", ""))
                b["task"] = ev.get("task", "")
                await _persist("streaming")
                yield frame("subagent_start", {"agent_id": ev.get("agent_id"), "name": ev.get("name"),
                                               "role": ev.get("role"), "task": ev.get("task")})
            elif et == "subagent_event":
                aid = ev.get("agent_id", "")
                inner = ev.get("event") or {}
                b = _sub_block(aid)
                it = inner.get("type")
                if it in ("text_delta", "thinking_delta"):
                    b["events"].append({"kind": "text" if it == "text_delta" else "reasoning",
                                        "text": inner.get("text", "")})
                elif it == "tool_start":
                    b["events"].append({"kind": "tool_start", "id": inner.get("id"),
                                        "name": inner.get("name"), "input": inner.get("input")})
                elif it == "tool_result":
                    b["events"].append({"kind": "tool_result", "id": inner.get("id"),
                                        "name": inner.get("name"), "result": inner.get("content"),
                                        "is_error": inner.get("is_error", False)})
                await _persist("streaming")
                yield frame("subagent_event", {"agent_id": aid, "event": inner})
            elif et == "subagent_status":
                # 关闭中的成员：让面板显示 stopping，而不是继续显示运行中
                aid = ev.get("agent_id", "")
                b = _sub_block(aid)
                b["status"] = ev.get("status", b.get("status", "running"))
                await _persist("streaming")
                yield frame("subagent_status", {"agent_id": aid, "name": ev.get("name"),
                                               "status": ev.get("status")})
            elif et == "subagent_end":
                aid = ev.get("agent_id", "")
                b = _sub_block(aid)
                b["status"] = ev.get("status", "completed")
                b["result"] = ev.get("result", "")
                b["turns"] = ev.get("turns", 0)
                if ev.get("error"):
                    b["error"] = ev.get("error")
                await _persist("streaming")
                yield frame("subagent_end", {"agent_id": aid, "name": ev.get("name"),
                                             "status": ev.get("status"), "result": ev.get("result", ""),
                                             "turns": ev.get("turns", 0), "error": ev.get("error", "")})
            elif et == "team_event":
                team_event = ev.get("event") or {}
                await _persist("streaming")
                yield frame("team_event", team_event)
            elif et == "error":
                terminal_status = "error"
                yield frame("error", {"error": ev.get("error", "unknown error")})
            elif et == "done":
                upstream_status = ev.get("status")
                if upstream_status in ("complete", "stopped"):
                    terminal_status = upstream_status

        status = terminal_status or ("stopped" if stop_event.is_set() else "complete")
        terminal_status = status
        _finalize_sub_blocks(status)
        await _persist(status, force=True)
        yield frame("done", {"message_id": message_id, "status": status, "model": ur.model.model_name})
    except asyncio.CancelledError:
        terminal_status = "stopped"
        _finalize_and_persist_after_cancel(
            _finalize_sub_blocks,
            lambda status: _persist(status, force=True),
            terminal_status,
        )
        raise
    except Exception as e:  # noqa: BLE001
        terminal_status = "error"
        _finalize_sub_blocks(terminal_status)
        await _persist(terminal_status, force=True)
        yield frame("error", {"error": f"{type(e).__name__}: {e}"})
    finally:
        # Cancellation-safe cleanup: synchronous releases happen before any
        # await, so a cancelled anyio scope cannot leak the Team quota.
        was_unknown = terminal_status is None
        if was_unknown:
            terminal_status = "stopped"
        cleanup_status = "stopped" if terminal_status in ("stopped", "error") else "completed"
        if turn is not None:
            try:
                await _cleanup_turn(turn, hub, user_id, session_id, generation_id,
                                    team_ctx.team_id, cleanup_status)
            except BaseException:
                logger.warning("turn cleanup failed", exc_info=True)
        if was_unknown:
            try:
                _finalize_and_persist_after_cancel(
                    _finalize_sub_blocks,
                    lambda status: _persist(status, force=True),
                    terminal_status,
                )
            except BaseException:
                logger.warning("persist terminal status failed", exc_info=True)
        unregister_generation(user_id, session_id, generation_id)
        try:
            await _release_or_background(
                lambda: team_store.release_generation(user_id, session_id, generation_id)
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            logger.warning("release generation lease failed", exc_info=True)


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, body: SendMessageIn, user: dict = Depends(get_current_user)):
    sess = await db.get_session(session_id, user["id"])
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    attachments = await _validate_attachments(user["id"], body.attachments)
    if not body.text.strip() and not attachments:
        raise HTTPException(status_code=400, detail="empty text")
    generation_id = body.generation_id or db.new_id("g")
    # One active generation per (user, session). Without this lease two tabs
    # can interleave writes into the same checkpoint, and stop becomes ambiguous.
    acquired = await team_store.acquire_generation(user["id"], session_id, generation_id)
    if not acquired:
        active = await team_store.get_active_generation(user["id"], session_id)
        # A DB lease is not proof that work is still running: the SSE task can
        # disappear after a client disconnect, proxy timeout or cancelled task.
        # Only the in-process task/reservation registry is authoritative.
        if active and not is_generation_live(user["id"], session_id, active):
            await team_store.converge_generation_facts(
                user["id"], session_id, active, status="stale"
            )
            await db.mark_stale_streaming_stopped(session_id)
            acquired = await team_store.acquire_generation(user["id"], session_id, generation_id)
            if not acquired:
                active = await team_store.get_active_generation(user["id"], session_id)
        if not acquired:
            raise HTTPException(status_code=409, detail={
                "error": "generation_active",
                "active_generation_id": active,
                "hint": "当前仍在生成。请先点击「停止」；若界面已失去连接，可刷新页面后重试。",
            })
    # Bridge the hand-off window before StreamingResponse starts iterating.
    reserve_generation(user["id"], session_id, generation_id)
    message_id = db.new_id("msg")
    count = await db.count_messages(session_id)
    if count == 0 and sess["title"] in ("新会话", "", None):
        await db.update_session(session_id, user["id"], title=body.text.strip().replace("\n", " ")[:24])
    async def guarded_stream():
        # Capture the task here, before the first await in _event_stream's
        # prelude, so the stop endpoint can cancel even a long compaction/build.
        stop_event = register_generation(user["id"], session_id, generation_id)
        try:
            async for chunk in _event_stream(
                user["id"], session_id, body.text, generation_id, message_id,
                sess.get("model_id", "") or "", stop_event=stop_event,
                attachments=attachments or None,
                reasoning_effort=(body.reasoning_effort if body.reasoning_effort is not None
                                  else sess.get("reasoning_effort", "") or ""),
            ):
                yield chunk
        finally:
            # _event_stream normally unregisters in its own finally; this is an
            # idempotent safety net for early returns (runtime unavailable,
            # model init failure, or a client disconnect before first yield).
            unregister_generation(user["id"], session_id, generation_id)
            release_generation_reservation(user["id"], session_id, generation_id)

    return StreamingResponse(
        guarded_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/sessions/{session_id}/stop", response_model=StopResult)
async def stop_generation(session_id: str, body: StopIn, user: dict = Depends(get_current_user)):
    sess = await db.get_session(session_id, user["id"])
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")

    # After a reconnect/refresh the client may not know generation_id. Resolve
    # the DB lease so stop still targets the actual running generation.
    generation_id = body.generation_id or await team_store.get_active_generation(user["id"], session_id)

    # Cooperative flag first, then the graceful ACP stop, and only after that
    # the hard kill.  Cancellation is what unblocks an LLM socket read or a tool
    # await, but it also runs the turn's synchronous cleanup
    # (``acp.stop_all_now``); cancelling before the graceful stop would therefore
    # preempt it and leave the ACP client with an unanswered prompt.
    n = 0
    try:
        n = request_stop(user["id"], session_id, generation_id)
    except TypeError:
        n = request_stop(session_id, generation_id)

    hub = get_hub()
    if hub is not None and generation_id:
        # Bounded: a wedged ACP client must never delay the hard kill below.
        stop_acp = getattr(hub, "stop_acp_generation", None)
        if callable(stop_acp):
            try:
                await asyncio.wait_for(
                    stop_acp(user["id"], session_id, generation_id), timeout=2.0)
            except Exception:
                pass

    if generation_id:
        try:
            cancel_generation(user["id"], session_id, generation_id)
        except Exception:
            pass

    # Cascade to subagents and OS processes regardless of hub availability.
    # Never tuck code_run cleanup behind generation_id/hub guards.
    subs = 0
    if hub is not None and generation_id:
        try:
            subs = await hub.stop_subagent_generation(user["id"], session_id, generation_id)
        except TypeError:
            try:
                subs = await hub.stop_subagent_generation(session_id, generation_id)
            except Exception:
                pass
        except Exception:
            pass
    if generation_id:
        try:
            await stop_code_runs(user["id"], session_id, generation_id)
        except Exception:
            pass

    stop_result = StopResult(ok=True, stopped=n, subagents_stopped=subs)
    target_message_id = body.message_id
    if not target_message_id:
        try:
            msgs = await db.list_messages(session_id)
            target_message_id = next(
                (m["id"] for m in reversed(msgs)
                 if m.get("role") == "assistant" and m.get("status") == "streaming"),
                None,
            )
        except Exception:
            target_message_id = None

    # Wait at most ~1s for the cancelled generator to persist its incremental
    # snapshot. This keeps stop responsive while preserving already-seen output.
    if target_message_id:
        for _ in range(10):
            try:
                msgs = await db.list_messages(session_id)
                msg = next((m for m in msgs if m["id"] == target_message_id), None)
                if msg and msg.get("status") in ("stopped", "complete", "error"):
                    stop_result.message = MessageOut(**msg)
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)

        # Hard fallback: a third-party await may ignore cancellation. The user
        # explicitly stopped, so never leave the UI/bubble in `streaming`.
        if stop_result.message is None:
            try:
                await db.update_message(target_message_id, status="stopped")
                msgs = await db.list_messages(session_id)
                msg = next((m for m in msgs if m["id"] == target_message_id), None)
                if msg:
                    stop_result.message = MessageOut(**msg)
            except Exception:
                pass

    # Explicit termination releases the lease now, not whenever a wedged
    # generator eventually unwinds. Otherwise the next send gets 409 forever.
    if generation_id:
        try:
            await team_store.converge_generation_facts(
                user["id"], session_id, generation_id, status="stopped"
            )
        except Exception:
            pass
        # Always call release as a final, idempotent safety net.  converge_*
        # already moved an active row to stopped; this also satisfies callers
        # that replaced release_generation in lightweight tests.
        try:
            await team_store.release_generation(user["id"], session_id, generation_id)
        except Exception:
            pass
        try:
            unregister_generation(user["id"], session_id, generation_id)
        except Exception:
            pass

    try:
        await db.mark_stale_streaming_stopped(session_id)
    except Exception:
        pass

    return stop_result



from pydantic import BaseModel


class CompactIn(BaseModel):
    instructions: str = ""
    keep: int | None = None


@router.post("/sessions/{session_id}/compact", response_model=CompactResult)
async def compact_session(session_id: str, body: CompactIn | None = None,
                          user: dict = Depends(get_current_user)):
    sess = await db.get_session(session_id, user["id"])
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    from ..compaction import manual_compact
    keep = body.keep if body and body.keep else 6
    instructions = body.instructions if body else ""
    result = await manual_compact(user["id"], session_id, keep=keep, instructions=instructions)
    return CompactResult(**result)


@router.get("/mcp/tools")
async def mcp_tools(user: dict = Depends(get_current_user)):
    """List the caller's tool list. Does NOT require a usable LLM model:
    a user may inspect/configure tools before adding a model."""
    hub = get_hub()
    if hub is None:
        return {"tools": []}
    try:
        ur = await hub.get_user_runtime(user["id"])
        return {"tools": [{"name": t.name, "description": t.description,
                           "input_schema": t.input_schema} for t in ur.registry.tools]}
    except Exception:
        # Model init failed (e.g. no model configured). Report built-in tools only.
        try:
            from ..builtin_tools import build_builtin_tools
            tools = build_builtin_tools(user["id"])
            return {"tools": [{"name": t.name, "description": t.description,
                               "input_schema": t.input_schema} for t in tools]}
        except Exception:
            return {"tools": []}


def _attachment_blocks(attachments: list[dict] | None) -> list[dict]:
    """把图片引用写进消息块，供前端回看；正文仍然是纯文本。"""
    if not attachments:
        return []
    return [{"type": "attachments", "items": list(attachments)}]


async def _validate_attachments(user_id: str, items) -> list[dict]:
    """校验引用确实属于当前用户的 uploads/，并补全 mime/size。"""
    out: list[dict] = []
    for item in items or []:
        try:
            path = uploads.resolve_attachment(user_id, item.path)
        except uploads.UploadError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
        out.append({
            "path": str(item.path).strip().lstrip("/"),
            "name": item.name or path.name,
            "mime": item.mime or uploads.MIME_BY_SUFFIX.get(path.suffix.lower(), ""),
            "size": path.stat().st_size,
        })
    if len(out) > uploads.MAX_IMAGES_PER_MESSAGE:
        raise HTTPException(
            status_code=400,
            detail=f"单条消息最多 {uploads.MAX_IMAGES_PER_MESSAGE} 张图片",
        )
    return out
