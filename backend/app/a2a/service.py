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

# 调用方可指定的同步窗口硬上限（秒）。见 `_clamp_sync_timeout`。
SYNC_TIMEOUT_CAP_S = 280.0


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


# 同步窗口用完时给调用方的说明。必须同时满足三件事：
#   1. 说清「还在跑，不是失败」—— 否则调用方会以为 Hlaudz 死了；
#   2. 说清去哪取进度（机器可读的 poll/subscribe + 人读的文案都要）；
#   3. 说清**不要重发**—— 重发会命中 messageId 幂等，拿回同一个任务，
#      但调用方若换了 messageId 就会真的跑第二遍，白烧一遍 token。
_STILL_RUNNING_TEXT = (
    "任务仍在运行，不是失败。同步窗口已到（INNER_A2A_SYNC_TIMEOUT_S），"
    "后台任务继续执行。"
    "用 GetTask(id) 查询当前状态，或用 SubscribeToTask(id) 订阅事件流直到终态。"
    "不要重新发送同一条消息。"
)


def _with_still_running(task: dict[str, Any]) -> dict[str, Any]:
    """给「提交了但还没结束」的返回补上可恢复指示。

    只在**提交语义**的返回上加（SendMessage / returnImmediately），
    不加在 GetTask 上：查询本身不需要「还在跑」这种提示，``state`` 已经说清了。
    """
    task_id = task["id"]
    status = dict(task.get("status") or {})
    if not status.get("message"):
        status["message"] = {
            "messageId": f"running_{task_id}",
            "role": "ROLE_AGENT",
            "parts": [{"text": _STILL_RUNNING_TEXT}],
        }
    return {
        **task,
        "status": status,
        # 机器可读：调用方不必解析自然语言
        "stillRunning": True,
        "poll": {"method": "GetTask", "params": {"id": task_id}},
        "subscribe": {"method": "SubscribeToTask", "params": {"id": task_id}},
    }


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
    # ---- 中断现场（2026-11-14）----
    # 每次工具边界写一次 DB，这样进程被 kill -9 也能留下「跑到第几步」。
    # 为什么不每个 token 都写：正文已经由 messages 表按 250ms 落库，
    # 这里只补「进度」这个 messages 表没有的维度，写太频纯属浪费。
    snap_steps = 0
    snap_tools = 0
    snap_last_tool = ""
    snap_dirty = False

    async def _flush_snapshot() -> None:
        nonlocal snap_dirty
        if not snap_dirty:
            return
        snap_dirty = False
        try:
            await store.update_snapshot(
                task_id, steps=snap_steps, tools_run=snap_tools,
                last_tool=snap_last_tool,
                partial_chars=sum(len(p) for p in text_parts),
            )
        except Exception:  # noqa: BLE001 - 现场记录失败不能影响主任务
            pass

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
                name = str(payload.get("name") or "")
                snap_steps += 1
                snap_last_tool = name
                snap_dirty = True
                await _flush_snapshot()
                events.publish(task_id, {"type": "tool_start",
                                         "id": payload.get("id"),
                                         "name": name})
            elif event == "tool_result":
                name = str(payload.get("name") or "")
                if not bool(payload.get("is_error")):
                    snap_tools += 1
                    snap_dirty = True
                    await _flush_snapshot()
                events.publish(task_id, {"type": "tool_result",
                                         "id": payload.get("id"),
                                         "name": name,
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
        # 收尾再写一次现场：工具边界那次算的 partial_chars 是**当时**的，
        # 之后的正文增量还没算进去。异常退出时这一次尤其重要 —— 它就是
        # 重启后（或本次 error 报告里）能说出「已生成多少」的唯一依据。
        # 注意此刻不要因为「任务已经结束」就跳过：结束不等于成功。
        snap_dirty = True
        await _flush_snapshot()
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


def _clamp_sync_timeout(value: Any) -> Optional[float]:
    """调用方可指定本次请求的同步等待窗口，但有硬上限。

    **为什么必须夹上限**：同步等待会一直占着这条 HTTP 连接。前面通常还有
    nginx / 网关 / 客户端自己的空闲超时（多为 60–100 秒），等待远超那个值
    只会让代理先断开 —— 调用方连 task id 都拿不到，而任务还在后台跑并占着
    配额，那才是真正的失控。所以默认值就短（60s），调用方也只能在
    ``SYNC_TIMEOUT_CAP_S`` 以内调，超出就夹到上限。

    ``None`` / 无法解析 → 返回 ``None``，表示「用服务端默认」。
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds != seconds or seconds <= 0:      # NaN / 非正数
        return None
    return min(seconds, SYNC_TIMEOUT_CAP_S)


# 配额判定的临界区锁（进程内）。见 `_enforce_quota` 的说明。
_QUOTA_LOCK = asyncio.Lock()


async def _enforce_quota(identity: dict[str, Any]) -> None:
    """并发配额：只拦新建任务，幂等重放不受影响。

    ⚠️ 调用方必须把它和「create_task」放进同一个临界区（``_QUOTA_LOCK``）：
    光是「先 count 再 create」在并发下等于没有配额 —— 三个请求会同时读到
    count=0 然后全部放行。2026-11-14 真机实测：串行第 3 个正确 429，
    **并发 3 个全部 200**。这是典型的 TOCTOU，单测只验串行所以一直没暴露。
    """
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
    # 配额判定与建任务必须在**同一个**临界区里完成，否则并发下配额形同虚设
    # （见 _enforce_quota 的说明）。临界区只包住「查数 → 建行」这两步，
    # 不包含前面的会话创建与后面的 spawn —— 那两步不改变活跃计数。
    if not session_id:
        session_id = await _resolve_session(identity, message, text)

    sess = await db.get_session(session_id, identity["user_id"])
    model_id = (sess or {}).get("model_id", "") or ""
    task_id = store.new_id("task")
    generation_id = store.new_id("g_a2a")
    level = normalize_policy(identity.get("tool_policy"))
    async with _QUOTA_LOCK:
        await _enforce_quota(identity)
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
    if existing:
        # 幂等重放：拿回的是已存在的任务。若它还没结束，同样要告诉调用方
        # 「还在跑、去哪取」—— 重放路径上尤其容易让人以为「没反应」。
        task = _to_a2a(row)
        return task if row["state"] in _TERMINAL else _with_still_running(task)
    if return_immediately:
        return _with_still_running(_to_a2a(row))

    timeout = config.sync_timeout_s() if sync_timeout is None else max(0.1, sync_timeout)
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        current = await store.get_task(identity["user_id"], identity["agent_id"], row["task_id"])
        if current and current["state"] in _TERMINAL:
            return _to_a2a(current)
        if asyncio.get_running_loop().time() >= deadline:
            # 超时不是失败：任务在后台继续跑，返回里带上 task id 与进度入口。
            return _with_still_running(_to_a2a(current or row))
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
        # 调用方可以在本次请求上指定同步窗口（配置键沿用 A2A 的
        # ``configuration`` 惯用法）。不传就用服务端默认；上限见 _clamp_sync_timeout。
        return await send_message(
            identity, params,
            return_immediately=bool(cfg.get("returnImmediately")),
            sync_timeout=_clamp_sync_timeout(cfg.get("syncTimeoutS")),
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
