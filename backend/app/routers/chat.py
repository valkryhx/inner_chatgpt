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

    try:
        async for ev in run_agent_stream(ur, session_id, text, stop_event):
            if ev["type"] == "text_delta":
                buffer.append(ev["text"])
                _append_text(ev["text"])
                yield frame("text_delta", {"text": ev["text"]})
            elif ev["type"] == "thinking_delta":
                reasoning_buf.append(ev["text"])
                _append_reasoning(ev["text"])
                yield frame("thinking_delta", {"text": ev["text"]})
            elif ev["type"] == "tool_start":
                step = {"type": "tool", "id": ev.get("id"), "name": ev.get("name"),
                        "input": ev.get("input")}
                tool_steps.append(step)
                blocks.append(step)
                yield frame("tool_start", {"id": ev.get("id"), "name": ev.get("name"), "input": ev.get("input")})
            elif ev["type"] == "chart":
                blocks.append({"type": "chart", "spec": ev["spec"]})
                yield frame("chart", {"spec": ev["spec"]})
            elif ev["type"] == "tool_result":
                for st in tool_steps:
                    if st["id"] == ev.get("id"):
                        st["result"] = ev.get("content")
                yield frame("tool_result", {"id": ev.get("id"), "name": ev.get("name"),
                                            "content": ev.get("content"), "is_error": ev.get("is_error", False)})

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
    return {"ok": True, "stopped": n}


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
