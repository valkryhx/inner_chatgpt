"""把 AgentLoop 的事件归一化为前端 SSE 事件。

事件协议（对齐 Aivory）：
  message_start / text_delta / thinking_delta / tool_start / tool_input /
  tool_result / chart / compaction / done / error

chart 事件来自工具返回的自描述 artifact（{"type":"chart",...}），
不依赖任何具体工具名。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator


def sse_frame(event_type: str, data: dict[str, Any], event_id: int | None = None) -> str:
    lines = [f"event: {event_type}"]
    lines.append("data: " + json.dumps(data, ensure_ascii=False))
    if event_id is not None:
        lines.append(f"id: {event_id}")
    return "\n".join(lines) + "\n\n"


def _tool_output_to_text(output: Any) -> str:
    content = getattr(output, "content", None)
    if content is None:
        return str(output)
    if isinstance(content, str):
        return content
    return str(content)


_ARTIFACT_TYPES = ("chart", "table")


def _json_objects_from_text(text: str) -> list[dict[str, Any]]:
    """从工具返回文本里提取「自描述 artifact」JSON 对象。

    约定（通用、不绑定任何工具名）：MCP 工具若返回形如 {"type":"chart", ...}
    的结果，平台就把它识别成可渲染 artifact。兼容三种常见包装：
      1) 纯 JSON
      2) MCP 文本内容列表 [{"type":"text","text":"<json>"}]
      3) {"content":[{"type":"text","text":"<json>"}]}
    """
    candidates: list[str] = []
    raw = text.strip()
    if not raw:
        return []
    candidates.append(raw)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                candidates.append(item["text"])
    elif isinstance(parsed, dict):
        content = parsed.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    candidates.append(item["text"])

    found: list[dict[str, Any]] = []
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("type") in _ARTIFACT_TYPES:
            found.append(obj)
    return found


def detect_artifacts(output_text: str) -> list[dict[str, Any]]:
    """公开入口：把工具输出里的自描述 artifact 抽出来（目前用于图表）。"""
    return _json_objects_from_text(output_text)


async def run_agent_stream(
    ur,
    session_id: str,
    user_text: str,
    stop_event: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Load history, append the user message, run the AgentLoop, yield normalized events.

    Event shapes match the frontend protocol:
      text_delta / thinking_delta / tool_start / tool_result / chart / done / error
    """
    store = getattr(ur, "store", None)
    user_id = str(getattr(ur, "user_id", "") or "")
    if store is None:
        # Compatibility with minimal test runtimes; production UserRuntime
        # always has a HistoryStore.
        history = []
    if store is not None:
        if user_id:
            history = await store.get(user_id, session_id)
        else:
            history = await store.get(session_id)
    history.append({"role": "user", "content": user_text})
    if store is not None:
        if user_id:
            await store.set(user_id, session_id, history)
        else:
            await store.set(session_id, history)

    async for ev in ur.loop.run_stream(history, stop_event=stop_event, session_id=session_id):
        et = ev.get("type")
        if et == "tool_result":
            content = ev.get("content", "") or ""
            yield ev
            for artifact in detect_artifacts(content):
                yield {"type": artifact["type"], "spec": artifact}
        elif et in ("text_delta", "thinking_delta", "tool_start", "done", "error"):
            yield ev
        else:
            yield ev
