"""A2A task orchestration on top of the existing Hlaudz chat/SSE runtime."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .. import db, team_store
from ..routers import chat
from ..runtime import release_generation_reservation, reserve_generation
from ..schemas import StopIn
from ..tool_policy import normalize as normalize_policy
from . import config, events, store, stream
from .errors import A2AError, invalid_params, task_not_cancelable, task_not_found

_TERMINAL = {
    "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED",
}
_BACKGROUND_TASKS: set[asyncio.Task] = set()
_TASK_HANDLES: dict[str, asyncio.Task] = {}


def _now_iso(ms: Optional[int] = None) -> str:
    ts = (ms if ms is not None else int(time.time() * 1000)) / 1000.0
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_frame(raw: str) -> tuple[str, dict[str, Any]]:
    event = ""
    data = ""
    for line in raw.splitlines():
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            data = line[6:]
    try:
        payload = json.loads(data) if data else {}
    except (TypeError, ValueError):
        payload = {}
    return event, payload


def _artifact(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    return [{
        "artifactId": "answer",
        "name": "answer",
        "parts": [{"text": text}],
    }]


def _status(row: dict[str, Any]) -> dict[str, Any]:
    status: dict[str, Any] = {
        "state": row.get("state") or "TASK_STATE_SUBMITTED",
        "timestamp": _now_iso(row.get("updated_at")),
    }
    if row.get("error"):
        status["message"] = {
            "messageId": f"err_{row['task_id']}",
            "role": "ROLE_AGENT",
            "parts": [{"text": row["error"]}],
        }
    return status


def _to_a2a(row: dict[str, Any], history_length: int = 0) -> dict[str, Any]:
    task = {
        "id": row["task_id"],
        "contextId": row["context_id"],
        "status": _status(row),
        "artifacts": row.get("artifacts") or [],
    }
    if history_length > 0:
        task["history"] = []  # filled by get_task when messages are available
    return task


def _extract_text(params: dict[str, Any]) -> tuple[dict[str, Any], str]:
    message = params.get("message")
    if not isinstance(message, dict):
        raise invalid_params("message is required")
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        raise invalid_params("message.parts is required")
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    text = "".join(chunks).strip()
    if not text:
        raise invalid_params("message must contain text")
    return message, text


async def _resolve_session(identity: dict[str, Any], message: dict[str, Any], text: str) -> str:
    context_id = message.get("contextId")
    if context_id:
        sess = await db.get_session(str(context_id), identity["user_id"])
        if not sess:
            raise A2AError(-32001, "context not found", data={"contextId": context_id})
        return str(context_id)
    title = text.replace("\n", " ")[:24] or "A2A session"
    sess = await db.create_session(identity["user_id"], title=title)
    return sess["id"]


async def _run_task(row: dict[str, Any], text: str, model_id: str,
                    tool_policy: str = "full") -> None:
    task_id = row["task_id"]
    started = False
    text_parts: list[str] = []
    error_text = ""
    done_status = ""
    terminal_state: Optional[str] = None
    try:
        acquired = await team_store.acquire_generation(
            row["user_id"], row["session_id"], row["generation_id"]
        )
        if not acquired:
            error_text = "generation_active"
            terminal_state = "TASK_STATE_REJECTED"
            return
        reserve_generation(row["user_id"], row["session_id"], row["generation_id"])
        started = True
        await store.update_task(task_id, state="TASK_STATE_WORKING")
        events.publish(task_id, {"type": "status", "state": "TASK_STATE_WORKING"})
        async for raw in chat._event_stream(
            row["user_id"], row["session_id"], text, row["generation_id"],
            row["message_id"], model_id, tool_policy=tool_policy,
        ):
            event, payload = _parse_frame(raw)
            if event == "text_delta":
                chunk = str(payload.get("text") or "")
                text_parts.append(chunk)
                events.publish(task_id, {"type": "text_delta", "text": chunk})
            elif event == "thinking_delta":
                events.publish(task_id, {"type": "thinking_delta",
                                         "text": str(payload.get("text") or "")})
            elif event == "tool_start":
                events.publish(task_id, {"type": "tool_start",
                                         "id": payload.get("id"),
                                         "name": payload.get("name") or ""})
            elif event == "tool_result":
                events.publish(task_id, {"type": "tool_result",
                                         "id": payload.get("id"),
                                         "name": payload.get("name") or "",
                                         "is_error": bool(payload.get("is_error"))})
            elif event == "error":
                error_text = str(payload.get("error") or "unknown error")
                events.publish(task_id, {"type": "error", "error": error_text})
            elif event == "done":
                done_status = str(payload.get("status") or "")
    except asyncio.CancelledError:
        terminal_state = "TASK_STATE_CANCELED"
        raise
    except Exception as exc:  # noqa: BLE001
        error_text = f"{type(exc).__name__}: {exc}"
    finally:
        if not started:
            release_generation_reservation(
                row["user_id"], row["session_id"], row["generation_id"]
            )
        content = "".join(text_parts)
        try:
            msg = await db.get_message(row["message_id"])
            if msg and msg.get("content"):
                content = msg["content"]
        except Exception:
            pass
        state = terminal_state
        if state is None:
            if error_text:
                state = "TASK_STATE_FAILED"
            elif done_status == "stopped":
                state = "TASK_STATE_CANCELED"
            else:
                state = "TASK_STATE_COMPLETED"
        if state in ("TASK_STATE_COMPLETED", "TASK_STATE_CANCELED"):
            error_text = ""
        try:
            await store.update_task(task_id, state=state, artifacts=_artifact(content),
                                    error=error_text)
        except Exception:
            pass
        events.publish(task_id, {"type": "terminal", "state": state,
                                 "error": error_text, "content": content})


def _spawn(row: dict[str, Any], text: str, model_id: str,
           tool_policy: str = "full") -> asyncio.Task:
    task = asyncio.create_task(_run_task(row, text, model_id, tool_policy))
    _BACKGROUND_TASKS.add(task)
    _TASK_HANDLES[row["task_id"]] = task

    def _done(t: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(t)
        if _TASK_HANDLES.get(row["task_id"]) is t:
            _TASK_HANDLES.pop(row["task_id"], None)

    task.add_done_callback(_done)
    return task


async def _enforce_quota(identity: dict[str, Any]) -> None:
    """并发配额：只拦新建任务，幂等重放不受影响。"""
    mine, total = await store.count_active(identity["user_id"])
    limit_user = config.max_active_per_user()
    if mine >= limit_user:
        raise A2AError(-32010, "too many active tasks for this agent",
                       http_status=429, data={"active": mine, "limit": limit_user})
    limit_global = config.max_active_global()
    if total >= limit_global:
        raise A2AError(-32010, "too many active tasks on this server",
                       http_status=429, data={"active": total, "limit": limit_global})


async def _prepare_task(identity: dict[str, Any], params: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    message, text = _extract_text(params)
    message_id = str(message.get("messageId") or store.new_id("amsg"))
    # A repeated messageId is an idempotency key. Without contextId there is
    # no session yet, so search by (agent, messageId) before creating one.
    if not message.get("contextId"):
        existing = await store.find_task_by_message_any(identity["agent_id"], message_id)
        if existing:
            return existing, True
    session_id = ""
    if message.get("contextId"):
        session_id = await _resolve_session(identity, message, text)
        existing = await store.find_task_by_message(identity["agent_id"], session_id, message_id)
        if existing:
            return existing, True
    await _enforce_quota(identity)
    if not session_id:
        session_id = await _resolve_session(identity, message, text)

    sess = await db.get_session(session_id, identity["user_id"])
    model_id = (sess or {}).get("model_id", "") or ""
    task_id = store.new_id("task")
    generation_id = store.new_id("g_a2a")
    level = normalize_policy(identity.get("tool_policy"))
    row = await store.create_task(
        task_id=task_id,
        context_id=session_id,
        user_id=identity["user_id"],
        agent_id=identity["agent_id"],
        session_id=session_id,
        message_id=message_id,
        generation_id=generation_id,
        tool_policy=level,
    )
    _spawn(row, text, model_id, level)
    return row, False


async def send_message(identity: dict[str, Any], params: dict[str, Any], *,
                       sync_timeout: Optional[float] = None,
                       return_immediately: bool = False) -> dict[str, Any]:
    row, existing = await _prepare_task(identity, params)
    if existing or return_immediately:
        return _to_a2a(row)

    timeout = config.sync_timeout_s() if sync_timeout is None else max(0.1, sync_timeout)
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        current = await store.get_task(identity["user_id"], identity["agent_id"], row["task_id"])
        if current and current["state"] in _TERMINAL:
            return _to_a2a(current)
        if asyncio.get_running_loop().time() >= deadline:
            return _to_a2a(current or row)
        await asyncio.sleep(0.05)


async def prepare_stream(identity: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Create/reuse a task and return its row; the caller subscribes to events."""
    row, _ = await _prepare_task(identity, params)
    return row


def _sse_result(req_id: Any, result: dict[str, Any]) -> str:
    payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _terminal_payloads(row: dict[str, Any]) -> list[dict[str, Any]]:
    content = ""
    for art in row.get("artifacts") or []:
        for part in art.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                content += part["text"]
    payloads: list[dict[str, Any]] = []
    if content:
        payloads.append(stream._artifact(row, content, True))
    payloads.append(stream._status(row, row.get("state") or "TASK_STATE_COMPLETED",
                                   row.get("error") or ""))
    return payloads


async def stream_row(row: dict[str, Any], req_id: Any):
    """Render one task as A2A SSE data frames.  Closing this generator does not cancel the task."""
    yield _sse_result(req_id, {"task": stream.task_payload(row)})
    task_id = row["task_id"]
    if row.get("state") in _TERMINAL:
        for payload in _terminal_payloads(row):
            yield _sse_result(req_id, payload)
        return

    q, replay, already_done = events.subscribe(task_id)
    sent_text = ""
    last_seq = 0
    try:
        for item in replay:
            if int(item.get("seq") or 0) <= last_seq:
                continue
            last_seq = int(item.get("seq") or 0)
            payloads, sent_text, terminal = stream.translate(row, item, sent_text)
            for payload in payloads:
                yield _sse_result(req_id, payload)
            if terminal:
                return
        if already_done:
            return
        while True:
            item = await q.get()
            seq = int(item.get("seq") or 0)
            if seq <= last_seq:
                continue
            last_seq = seq
            payloads, sent_text, terminal = stream.translate(row, item, sent_text)
            for payload in payloads:
                yield _sse_result(req_id, payload)
            if terminal:
                return
    finally:
        events.unsubscribe(task_id, q)


async def get_task(identity: dict[str, Any], task_id: str,
                   history_length: int = 0) -> Optional[dict[str, Any]]:
    row = await store.get_task(identity["user_id"], identity["agent_id"], task_id)
    if row is None:
        return None
    result = _to_a2a(row)
    if history_length > 0:
        try:
            messages = await db.list_messages(row["session_id"])
        except Exception:
            messages = []
        history = []
        for msg in messages[-max(0, history_length):]:
            role = "ROLE_USER" if msg.get("role") == "user" else "ROLE_AGENT"
            history.append({
                "messageId": msg["id"],
                "role": role,
                "parts": [{"text": msg.get("content") or ""}],
            })
        result["history"] = history
    return result


async def list_tasks(identity: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    rows = await store.list_tasks(
        identity["user_id"], identity["agent_id"],
        context_id=str(params.get("contextId") or ""),
        state=str(params.get("status") or ""),
        limit=int(params.get("pageSize") or 50),
    )
    return {"tasks": [_to_a2a(r) for r in rows], "nextPageToken": ""}


async def cancel_task(identity: dict[str, Any], task_id: str) -> dict[str, Any]:
    row = await store.get_task(identity["user_id"], identity["agent_id"], task_id)
    if row is None:
        raise task_not_found(task_id)
    if row["state"] in _TERMINAL:
        raise task_not_cancelable(task_id)
    user = identity.get("user") or await db.get_user(identity["user_id"])
    if not user:
        raise task_not_found(task_id)
    await chat.stop_generation(
        row["session_id"],
        StopIn(generation_id=row["generation_id"], message_id=row["message_id"]),
        user,
    )
    handle = _TASK_HANDLES.get(task_id)
    if handle is not None and not handle.done():
        handle.cancel()
    updated = await store.update_task(task_id, state="TASK_STATE_CANCELED", error="")
    return _to_a2a(updated or row)


async def handle_method(identity: dict[str, Any], method: str,
                        params: dict[str, Any]) -> dict[str, Any]:
    if method == "SendMessage":
        cfg = params.get("configuration") or {}
        return await send_message(
            identity, params,
            return_immediately=bool(cfg.get("returnImmediately")),
        )
    if method == "GetTask":
        task_id = str(params.get("id") or "")
        if not task_id:
            raise invalid_params("id is required")
        result = await get_task(identity, task_id, int(params.get("historyLength") or 0))
        if result is None:
            raise task_not_found(task_id)
        return result
    if method == "ListTasks":
        return await list_tasks(identity, params)
    if method == "CancelTask":
        task_id = str(params.get("id") or "")
        if not task_id:
            raise invalid_params("id is required")
        return await cancel_task(identity, task_id)
    from .errors import method_not_found
    raise method_not_found(method)
