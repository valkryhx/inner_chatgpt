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
                 max_turns: int = 300, history_store=None, *, user_id: str = "",
                 message_injector=None):
        self.client = client
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.history_store = history_store  # optional; when set, persists history
        # Owner identity is server-generated.  It is never accepted from a tool
        # argument or model output.
        self.user_id = user_id
        # 可选：每轮模型调用前的消息注入器（子代理 steer 用）
        self.message_injector = message_injector

    # ---------- sanitation: repair dangling tool_calls ----------
    async def sanitize(self, history: list[dict]) -> bool:
        """Repair tool-call protocol leftovers before a new model turn.

        A stopped turn can leave a ``role=tool`` message without its preceding
        assistant ``tool_calls`` message, or leave part of a multi-call block
        unanswered.  OpenAI-compatible gateways may report that malformed
        history as the misleading error "current model does not support
        function calling".  Remove orphan results and incomplete blocks; replay
        known missing tools only when the incomplete block is the tail.
        Returns True if ``history`` was changed (caller should persist it).
        """
        if not history:
            return False
        changed = False

        def call_ids(msg: dict) -> list[str]:
            return [
                str(tc.get("id"))
                for tc in (msg.get("tool_calls") or [])
                if tc.get("id")
            ]

        def drop_orphan_tool_messages() -> None:
            nonlocal changed
            known = {
                cid
                for msg in history
                if msg.get("role") == "assistant"
                for cid in call_ids(msg)
            }
            kept = [
                msg for msg in history
                if msg.get("role") != "tool" or str(msg.get("tool_call_id") or "") in known
            ]
            if len(kept) != len(history):
                history[:] = kept
                changed = True

        drop_orphan_tool_messages()
        i = 0
        while i < len(history):
            msg = history[i]
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                i += 1
                continue

            wanted = call_ids(msg)
            if not wanted:
                del history[i]
                changed = True
                continue

            j = i + 1
            while j < len(history) and history[j].get("role") == "tool":
                j += 1

            have = [str(history[k].get("tool_call_id") or "") for k in range(i + 1, j)]
            missing = [cid for cid in wanted if cid not in have]
            if not missing:
                # Keep exactly one result for each call.  Duplicate results are
                # also invalid even when the call id itself is known.
                seen: set[str] = set()
                block: list[dict] = []
                for k in range(i + 1, j):
                    cid = str(history[k].get("tool_call_id") or "")
                    if cid in wanted and cid not in seen:
                        block.append(history[k])
                        seen.add(cid)
                if len(block) != j - i - 1:
                    history[i + 1:j] = block
                    changed = True
                    i += 1 + len(block)
                else:
                    i = j
                continue

            has_later_non_tool = any(m.get("role") != "tool" for m in history[j:])
            names = set(self.registry.names()) if self.registry is not None else set()
            can_replay = all(
                (tc.get("function") or {}).get("name") in names
                for tc in (msg.get("tool_calls") or [])
                if str(tc.get("id") or "") in missing
            )
            if not has_later_non_tool and can_replay:
                for tc in msg.get("tool_calls") or []:
                    tid = str(tc.get("id") or "")
                    if tid not in missing:
                        continue
                    name = (tc.get("function") or {}).get("name")
                    args = _safe_json((tc.get("function") or {}).get("arguments"))
                    out = await self.registry.dispatch(name, args)
                    history.append({
                        "role": "tool",
                        "tool_call_id": tid,
                        "name": name,
                        "content": out,
                    })
                changed = True
                i = len(history)
                continue

            # Keep neither half of an incomplete exchange.  Leaving the tool
            # result behind is what makes the next request invalid.
            del history[i:j]
            changed = True
            # Inspect the item that shifted into this index.

        # A dropped incomplete assistant block may have left a result whose
        # call was removed above.
        drop_orphan_tool_messages()
        return changed

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
                if self.user_id:
                    await self.history_store.set(self.user_id, session_id, history)
                else:
                    await self.history_store.set(session_id, history)
        except Exception:
            pass

        turn = 0
        while turn < self.max_turns:
            if stopped():
                yield {"type": "done", "status": "stopped"}
                return
            turn += 1
            if self.message_injector is not None:
                try:
                    injected = self.message_injector() or []
                except Exception:
                    injected = []
                for msg in injected:
                    history.append(msg)
                    yield {"type": "message_injected", "text": msg.get("content", "")}
            # streaming deltas must be yielded immediately; run() collects them.
            # We use a queue to bridge on_delta callback -> async yields.
            q: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def on_delta_q(kind: str, text: str) -> None:
                loop.call_soon_threadsafe(q.put_nowait, (kind, text))

            async def _drive():
                try:
                    # 图片只在「送模型的那一份」里展开成 data URL：
                    # history 本身要保持纯文本，否则 base64 会被持久化进 session_history。
                    wire = history
                    if self.user_id:
                        try:
                            from .uploads import expand_images_for_wire
                            wire = expand_images_for_wire(history, self.user_id)
                        except Exception:
                            wire = history
                    res = await self.client.run(wire, self.registry.schemas(),
                                                on_delta=on_delta_q, stop_check=stopped)
                    return res
                finally:
                    loop.call_soon_threadsafe(q.put_nowait, ("__end__", None))

            drive_task = asyncio.create_task(_drive())
            result: Optional[ChatResult] = None
            try:
                while True:
                    kind, text = await q.get()
                    if kind == "__end__":
                        break
                    if kind == "reasoning":
                        yield {"type": "thinking_delta", "text": text}
                    else:
                        yield {"type": "text_delta", "text": text}
                result = await drive_task
            except LLMError as e:
                yield {"type": "error", "error": str(e)}
                return
            except asyncio.CancelledError:
                # Cancelling the outer SSE task must also stop the inner LLM
                # request.  Otherwise the model call keeps running in the
                # background and can mutate history after the user stopped.
                if not drive_task.done():
                    drive_task.cancel()
                try:
                    await drive_task
                except BaseException:
                    pass
                raise
            finally:
                if not drive_task.done():
                    drive_task.cancel()

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
                    if self.user_id:
                        await self.history_store.set(self.user_id, session_id, history)
                    else:
                        await self.history_store.set(session_id, history)
                yield {"type": "done", "status": "complete"}
                return

            # append assistant(tool_calls) + run each tool
            amsg = {"role": "assistant", "content": content, "tool_calls": tool_calls}
            if reasoning:
                amsg["reasoning"] = reasoning
            history.append(amsg)
            prepared = []
            for tc in tool_calls:
                if stopped():
                    break
                fid = tc.get("id") or ""
                name = tc.get("function", {}).get("name", "")
                args = _safe_json(tc.get("function", {}).get("arguments"))
                prepared.append({"id": fid, "name": name, "args": args})
                yield {"type": "tool_start", "id": fid, "name": name, "input": args}
            if prepared and not stopped():
                # 相邻的只读工具并发，写类与 code_run 串行（tool_batching 保证顺序）
                from .tool_batching import run_batched

                outs = await run_batched(prepared, self.registry.dispatch)
                for item, out in zip(prepared, outs):
                    is_err = out.startswith("[工具执行错误]")
                    history.append({"role": "tool", "tool_call_id": item["id"],
                                    "name": item["name"], "content": out})
                    yield {"type": "tool_result", "id": item["id"], "name": item["name"],
                           "content": out, "is_error": is_err}
                    if self.history_store is not None:
                        if self.user_id:
                            await self.history_store.set(self.user_id, session_id, history)
                        else:
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
