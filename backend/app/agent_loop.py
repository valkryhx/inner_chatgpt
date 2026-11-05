"""Self-built agent loop (replaces langchain create_agent + langgraph astream_events).

A plain async while-loop:
  model -> if tool_calls: dispatch each tool -> append tool results -> call model again
  -> until model returns no tool calls / max_turns / stopped.

Emits normalized events via async generator:
  {"type":"text_delta","text":...}
  {"type":"thinking_delta","text":...}
  {"type":"tool_start","id","name","input"}
  {"type":"tool_result","id","name","content","is_error"}
The SSE layer (sse.py) maps these + chart detection into the frontend protocol.

Design reference: GenericAgent genericagent/core/loop.py::agent_runner_loop.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Optional

from .llm_client import ChatResult, LLMClient, LLMError
from .tools import ToolRegistry


def _dump(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


class AgentLoop:
    def __init__(self, client: LLMClient, registry: ToolRegistry, system_prompt: str,
                 max_turns: int = 48, history_store=None):
        self.client = client
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.history_store = history_store  # optional; when set, persists history

    # ---------- sanitation: repair dangling tool_calls ----------
    async def sanitize(self, history: list[dict]) -> bool:
        """If the tail assistant message has tool_calls lacking matching tool
        results, repair: re-run known missing tools, else drop that assistant msg.
        Returns True if changed (caller persists)."""
        if not history:
            return False
        last_ai_idx = None
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("role") == "assistant" and history[i].get("tool_calls"):
                last_ai_idx = i
                break
        if last_ai_idx is None:
            return False
        ai = history[last_ai_idx]
        wanted = {tc.get("id"): tc for tc in ai.get("tool_calls", []) if tc.get("id")}
        have = {m.get("tool_call_id") for m in history[last_ai_idx + 1:] if m.get("role") == "tool"}
        missing = {tid: tc for tid, tc in wanted.items() if tid not in have}
        if not missing:
            return False
        has_later_non_tool = any(m.get("role") != "tool" for m in history[last_ai_idx + 1:])
        names = set(self.registry.names())
        can_replay = all((tc.get("function", {}).get("name") in names) for tc in missing.values())
        if not has_later_non_tool and can_replay:
            for tid, tc in missing.items():
                name = tc.get("function", {}).get("name")
                args = _safe_json(tc.get("function", {}).get("arguments"))
                out = await self.registry.dispatch(name, args)
                history.append({"role": "tool", "tool_call_id": tid, "name": name, "content": out})
            return True
        del history[last_ai_idx]
        return True

    # ---------- main loop ----------
    async def run_stream(
        self,
        history: list[dict],
        stop_event: Optional[asyncio.Event] = None,
        session_id: str = "",
    ) -> AsyncIterator[dict]:
        """Mutates `history` in place (appends assistant/tool messages). Emits events.
        The caller is responsible for appending the trailing user message first."""
        def stopped() -> bool:
            return bool(stop_event is not None and stop_event.is_set())

        # repair any dangling state before a new turn
        try:
            changed = await self.sanitize(history)
            if changed and self.history_store is not None:
                await self.history_store.set(session_id, history)
        except Exception:
            pass

        turn = 0
        while turn < self.max_turns:
            if stopped():
                yield {"type": "done", "status": "stopped"}
                return
            turn += 1
            # streaming deltas must be yielded immediately; run() collects them.
            # We use a queue to bridge on_delta callback -> async yields.
            q: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def on_delta_q(kind: str, text: str) -> None:
                loop.call_soon_threadsafe(q.put_nowait, (kind, text))

            async def _drive():
                try:
                    res = await self.client.run(history, self.registry.schemas(),
                                                on_delta=on_delta_q, stop_check=stopped)
                    return res
                finally:
                    loop.call_soon_threadsafe(q.put_nowait, ("__end__", None))

            drive_task = asyncio.create_task(_drive())
            result: Optional[ChatResult] = None
            while True:
                kind, text = await q.get()
                if kind == "__end__":
                    break
                if kind == "reasoning":
                    yield {"type": "thinking_delta", "text": text}
                else:
                    yield {"type": "text_delta", "text": text}
            try:
                result = await drive_task
            except LLMError as e:
                yield {"type": "error", "error": str(e)}
                return

            if stopped():
                yield {"type": "done", "status": "stopped"}
                return

            content = result.content if result else ""
            reasoning = result.reasoning if result else ""
            tool_calls = result.tool_calls if result else []

            if not tool_calls:
                history.append({"role": "assistant", "content": content,
                                **({"reasoning": reasoning} if reasoning else {})})
                if self.history_store is not None:
                    await self.history_store.set(session_id, history)
                yield {"type": "done", "status": "complete"}
                return

            # append assistant(tool_calls) + run each tool
            amsg = {"role": "assistant", "content": content, "tool_calls": tool_calls}
            if reasoning:
                amsg["reasoning"] = reasoning
            history.append(amsg)
            for tc in tool_calls:
                if stopped():
                    break
                fid = tc.get("id") or ""
                name = tc.get("function", {}).get("name", "")
                args = _safe_json(tc.get("function", {}).get("arguments"))
                yield {"type": "tool_start", "id": fid, "name": name, "input": args}
                out = await self.registry.dispatch(name, args)
                is_err = out.startswith("[工具执行错误]")
                history.append({"role": "tool", "tool_call_id": fid, "name": name, "content": out})
                yield {"type": "tool_result", "id": fid, "name": name, "content": out, "is_error": is_err}
                if self.history_store is not None:
                    await self.history_store.set(session_id, history)

        yield {"type": "error", "error": f"达到最大轮数 {self.max_turns}，已停止"}


def _safe_json(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_raw": raw}
    except Exception:
        return {"_raw": raw}
