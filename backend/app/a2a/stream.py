"""Translate internal task events into A2A v1.0 StreamResponse objects."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional


def _iso(ms: Optional[int] = None) -> str:
    ts = (ms if ms is not None else int(time.time() * 1000)) / 1000.0
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def task_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["task_id"],
        "contextId": row["context_id"],
        "status": {
            "state": row.get("state") or "TASK_STATE_SUBMITTED",
            "timestamp": _iso(row.get("updated_at")),
        },
        "artifacts": row.get("artifacts") or [],
    }


def _status(row: dict[str, Any], state: str, text: str = "", seq: int = 0) -> dict[str, Any]:
    status: dict[str, Any] = {"state": state, "timestamp": _iso()}
    if text:
        status["message"] = {
            "messageId": f"a2a-{row['task_id']}-{seq or 1}",
            "role": "ROLE_AGENT",
            "parts": [{"text": text}],
        }
    return {
        "statusUpdate": {
            "taskId": row["task_id"],
            "contextId": row["context_id"],
            "status": status,
        }
    }


def _artifact(row: dict[str, Any], text: str, last: bool) -> dict[str, Any]:
    return {
        "artifactUpdate": {
            "taskId": row["task_id"],
            "contextId": row["context_id"],
            "artifact": {
                "artifactId": "answer",
                "name": "answer",
                "parts": [{"text": text}] if text else [],
            },
            "append": True,
            "lastChunk": last,
            "index": 0,
        }
    }


def translate(row: dict[str, Any], item: dict[str, Any], sent_text: str) -> tuple[list[dict[str, Any]], str, bool]:
    """Return (StreamResponse list, new_sent_text, is_terminal)."""
    event = item.get("event") or {}
    kind = event.get("type")
    seq = int(item.get("seq") or 0)
    if kind == "text_delta":
        text = str(event.get("text") or "")
        return [_artifact(row, text, False)], sent_text + text, False
    if kind == "status":
        return [_status(row, str(event.get("state") or "TASK_STATE_WORKING"))], sent_text, False
    if kind == "tool_start":
        return [_status(row, "TASK_STATE_WORKING",
                        f"工具 {event.get('name') or 'unknown'} 开始", seq)], sent_text, False
    if kind == "tool_result":
        ok = not event.get("is_error")
        return [_status(row, "TASK_STATE_WORKING",
                        f"工具 {event.get('name') or 'unknown'} {'完成' if ok else '失败'}", seq)], sent_text, False
    if kind == "thinking_delta":
        # Do not turn every reasoning token into an A2A status message.
        return [], sent_text, False
    if kind == "error":
        return [], sent_text, False
    if kind == "terminal":
        content = str(event.get("content") or "")
        remaining = content[len(sent_text):] if content.startswith(sent_text) else ""
        if not sent_text:
            remaining = content
        state = str(event.get("state") or "TASK_STATE_COMPLETED")
        err = str(event.get("error") or "")
        payloads: list[dict[str, Any]] = []
        if remaining:
            payloads.append(_artifact(row, remaining, True))
        payloads.append(_status(row, state, err, seq))
        return payloads, content, True
    return [], sent_text, False
