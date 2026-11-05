"""聊天路由（多用户）：SSE 流式生成、停止、手动压缩、MCP 清单。"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import db
from ..auth import get_current_user
from ..runtime import get_hub, register_generation, request_stop, unregister_generation
from ..schemas import CompactResult, SendMessageIn, StopIn
from ..sse import run_agent_stream, sse_frame
from ..subagent.context import AgentContext
from ..subagent.session import build_turn_runtime

_SUBAGENT_EVENT_ID = "__subagent__"


async def _merged_events(main_gen, buffer, host):
    """把主代理事件流与子代理事件流合并成一条（顺序 = 真实发生顺序）。"""
    q: asyncio.Queue = asyncio.Queue()

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
    try:
        while True:
            ev = await q.get()
            if ev.get("type") == _SUBAGENT_EVENT_ID:
                break
            yield ev
        # 主循环结束：若还有未消费的子代理事件，短暂排空后收尾
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

router = APIRouter(prefix="/api", tags=["chat"])


async def _event_stream(user_id: str, session_id: str, text: str, generation_id: str, message_id: str,
                        model_id: str = "") -> AsyncIterator[str]:
    hub = get_hub()
    if hub is None:
        yield sse_frame("error", {"error": "runtime not ready"})
        return
    try:
        ur = await hub.get_user_runtime(user_id, model_id)
    except Exception as e:  # noqa: BLE001
        # 典型场景：全新部署、用户还没配置任何模型（且仓库不携带 llm.yaml）。
        # 这里以 SSE 事件返回可读提示，而不是让前端只看到 500。
        yield sse_frame("error", {
            "error": f"无法初始化模型：{type(e).__name__}: {e}",
            "hint": "请先在「设置 -> 我的模型」新增模型（base_url / api_key / model）并设为默认。",
        })
        return

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

    await db.add_message(session_id, "user", content=text)
    await db.add_message(session_id, "assistant", content="", status="streaming", message_id=message_id)

    stop_event = register_generation(session_id, generation_id)
    buffer: list[str] = []
    reasoning_buf: list[str] = []
    tool_steps: list[dict[str, Any]] = []
    # 消息块按「事件真实发生顺序」累积：文本增量并入当前文本块，
    # 工具/图表在出现的位置插入。这样刷新后顺序与流式时一致。
    blocks: list[dict[str, Any]] = []
    event_id = 0
    persisted = False

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

    async def _persist(status: str) -> None:
        nonlocal persisted
        if persisted:
            return
        persisted = True
        content = "".join(buffer)
        try:
            await db.update_message(message_id, content=content, reasoning="".join(reasoning_buf),
                                    blocks=blocks, status=status)
        except Exception:
            pass

    yield frame("message_start", {"message_id": message_id})

    # ---- 子代理：本轮派生的 Runtime（含 5 个编排工具）+ 子代理事件合并进同一条 SSE ----
    ctx = AgentContext(user_id=user_id, session_id=session_id,
                       message_id=message_id, generation_id=generation_id)
    turn = None
    try:
        turn = build_turn_runtime(ur, ctx)
        hub.register_subagent_host(session_id, generation_id, turn.host)
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

    try:
        if turn is not None:
            events = _merged_events(run_agent_stream(_TurnUR(turn), session_id, text, stop_event),
                                    turn.buffer, turn.host)
        else:
            events = run_agent_stream(ur, session_id, text, stop_event)

        async for ev in events:
            et = ev["type"]
            if et == "text_delta":
                buffer.append(ev["text"])
                _append_text(ev["text"])
                yield frame("text_delta", {"text": ev["text"]})
            elif et == "thinking_delta":
                reasoning_buf.append(ev["text"])
                _append_reasoning(ev["text"])
                yield frame("thinking_delta", {"text": ev["text"]})
            elif et == "tool_start":
                step = {"type": "tool", "id": ev.get("id"), "name": ev.get("name"),
                        "input": ev.get("input")}
                tool_steps.append(step)
                blocks.append(step)
                yield frame("tool_start", {"id": ev.get("id"), "name": ev.get("name"),
                                           "input": ev.get("input")})
            elif et == "chart":
                blocks.append({"type": "chart", "spec": ev["spec"]})
                yield frame("chart", {"spec": ev["spec"]})
            elif et == "tool_result":
                for st in tool_steps:
                    if st["id"] == ev.get("id"):
                        st["result"] = ev.get("content")
                yield frame("tool_result", {"id": ev.get("id"), "name": ev.get("name"),
                                            "content": ev.get("content"),
                                            "is_error": ev.get("is_error", False)})
            elif et == "subagent_start":
                b = _sub_block(ev.get("agent_id", ""), ev.get("name", ""), ev.get("role", ""))
                b["task"] = ev.get("task", "")
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
                yield frame("subagent_event", {"agent_id": aid, "event": inner})
            elif et == "subagent_end":
                aid = ev.get("agent_id", "")
                b = _sub_block(aid)
                b["status"] = ev.get("status", "completed")
                b["result"] = ev.get("result", "")
                b["turns"] = ev.get("turns", 0)
                if ev.get("error"):
                    b["error"] = ev.get("error")
                yield frame("subagent_end", {"agent_id": aid, "name": ev.get("name"),
                                             "status": ev.get("status"), "result": ev.get("result", ""),
                                             "turns": ev.get("turns", 0), "error": ev.get("error", "")})
            elif et == "error":
                yield frame("error", {"error": ev.get("error", "unknown error")})

        status = "stopped" if stop_event.is_set() else "complete"
        await _persist(status)
        yield frame("done", {"message_id": message_id, "status": status, "model": ur.model.model_name})
    except asyncio.CancelledError:
        await _persist("stopped")
        raise
    except Exception as e:  # noqa: BLE001
        await _persist("error")
        yield frame("error", {"error": f"{type(e).__name__}: {e}"})
    finally:
        if turn is not None:
            try:
                await turn.host.close_all()
            except Exception:
                pass
            try:
                hub.unregister_subagent_host(session_id, generation_id)
            except Exception:
                pass
        await _persist("stopped")
        unregister_generation(session_id, generation_id)


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, body: SendMessageIn, user: dict = Depends(get_current_user)):
    sess = await db.get_session(session_id, user["id"])
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="empty text")
    generation_id = body.generation_id or db.new_id("g")
    message_id = db.new_id("msg")
    count = await db.count_messages(session_id)
    if count == 0 and sess["title"] in ("新会话", "", None):
        await db.update_session(session_id, user["id"], title=body.text.strip().replace("\n", " ")[:24])
    return StreamingResponse(
        _event_stream(user["id"], session_id, body.text, generation_id, message_id, sess.get("model_id", "") or ""),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/sessions/{session_id}/stop")
async def stop_generation(session_id: str, body: StopIn, user: dict = Depends(get_current_user)):
    sess = await db.get_session(session_id, user["id"])
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    n = request_stop(session_id, body.generation_id)
    # 级联停止子代理：否则主代理停了、子代理还在后台继续烧钱（设计文档 §9.1）
    subs = 0
    try:
        hub = get_hub()
        if hub is not None and body.generation_id:
            subs = await hub.stop_subagent_generation(session_id, body.generation_id)
    except Exception:
        pass
    return {"ok": True, "stopped": n, "subagents_stopped": subs}


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
