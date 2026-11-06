"""Self-built LLM client (no langchain). httpx async, two wires:
  - openai_chat      -> POST {base}/chat/completions
  - openai_responses -> POST {base}/responses
Streams text/reasoning deltas and returns normalized tool_calls + usage.
Design reference: GenericAgent genericagent/llm/core.py (_openai_stream/_parse_openai_sse).
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

_RESP_CACHE_KEY = str(uuid.uuid4())
REASONING_MARK = "@@reasoning@@"


class LLMError(Exception):
    """Model request failed (provider error / HTTP failure)."""


@dataclass
class ChatResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # [{id,type,function:{name,arguments}}]
    usage: Optional[dict] = None


@dataclass
class LLMSettings:
    """Flat runtime settings needed to talk to one model endpoint."""
    model: str
    base_url: str
    api_key: str
    wire_api: str = "openai_chat"
    temperature: float = 1.0
    max_tokens: Optional[int] = 8192
    timeout: int = 120
    max_retries: int = 2
    reasoning_effort: Optional[str] = None
    system_prompt: str = ""

    @property
    def api_mode(self) -> str:
        return "responses" if (self.wire_api or "").strip() == "openai_responses" else "chat_completions"


def _auto_url(base: str, path: str) -> str:
    b, p = (base or "").rstrip("/"), path.strip("/")
    if not b:
        raise LLMError("base_url is empty")
    if b.endswith(p):
        return b
    return f"{b}/{p}" if re.search(r"/v\d+(/|$)", b) else f"{b}/v1/{p}"


def _try_parse_args(raw: str) -> list[dict]:
    """Parse tool args JSON; split concatenated objects like {..}{..} if needed."""
    if not raw:
        return [{}]
    try:
        return [json.loads(raw)]
    except Exception:
        pass
    parts = re.split(r"(?<=\})(?=\{)", raw)
    if len(parts) > 1:
        out = []
        for p in parts:
            try:
                out.append(json.loads(p))
            except Exception:
                return [{"_raw": raw}]
        return out
    return [{"_raw": raw}]


def _normalize_usage(usage: dict, api_mode: str) -> Optional[dict]:
    if not usage:
        return None
    def _i(v):
        try:
            return max(0, int(v or 0))
        except Exception:
            return 0
    if api_mode == "responses":
        inp, out = _i(usage.get("input_tokens")), _i(usage.get("output_tokens"))
        total = _i(usage.get("total_tokens")) or inp + out
        cached = _i((usage.get("input_tokens_details") or {}).get("cached_tokens"))
    else:
        inp, out = _i(usage.get("prompt_tokens")), _i(usage.get("completion_tokens"))
        total = _i(usage.get("total_tokens")) or inp + out
        cached = _i((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
    if inp == 0 and out == 0 and total == 0 and cached == 0:
        return None
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": total, "cached_tokens": cached}


def _provider_error(evt: dict) -> Optional[str]:
    """Most proxies return a bare {"error": {...}}; surface it instead of dropping."""
    if not isinstance(evt, dict) or "error" not in evt:
        return None
    err = evt.get("error")
    if isinstance(err, dict):
        return err.get("message") or err.get("type") or "provider error"
    return str(err or "provider error")


def _sse_payload(line: str) -> Optional[str]:
    if line.startswith("data:"):
        return line[5:].lstrip()
    s = line.strip()
    if s.startswith("{") and '"error"' in s:
        return s
    return None


def _to_chat_messages(messages: list[dict], system_prompt: str) -> list[dict]:
    out: list[dict] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    for m in messages:
        role = m.get("role", "user")
        if role == "system":
            out.append({"role": "system", "content": m.get("content", "")})
            continue
        if role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id", ""),
                "content": m.get("content", "") if isinstance(m.get("content"), str) else str(m.get("content", "")),
            })
            continue
        item: dict[str, Any] = {"role": role}
        content = m.get("content", "")
        item["content"] = content if isinstance(content, str) else str(content or "")
        if role == "assistant":
            if m.get("reasoning"):
                item["reasoning_content"] = m["reasoning"]
            if m.get("tool_calls"):
                item["tool_calls"] = m["tool_calls"]
                if not item["content"]:
                    item["content"] = "."
        out.append(item)
    return out


def _to_responses_input(messages: list[dict]) -> list[dict]:
    result: list[dict] = []
    for m in messages:
        role = str(m.get("role", "user")).lower()
        if role == "tool":
            result.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id", ""),
                "output": m.get("content", "") if isinstance(m.get("content"), str) else str(m.get("content", "")),
            })
            continue
        if role == "system":
            role = "developer"
        if role not in ("user", "assistant", "developer"):
            role = "user"
        content = m.get("content", "")
        parts = []
        text = content if isinstance(content, str) else str(content or "")
        if text:
            parts.append({"type": "output_text" if role == "assistant" else "input_text", "text": text})
        if not parts:
            parts = [{"type": "output_text" if role == "assistant" else "input_text", "text": "."}]
        result.append({"role": role, "content": parts})
        for tc in (m.get("tool_calls") or []):
            f = tc.get("function", {})
            result.append({
                "type": "function_call",
                "call_id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                "name": f.get("name", ""),
                "arguments": f.get("arguments", "{}"),
            })
    return result


def _prepare_tools(tools: Optional[list[dict]], api_mode: str) -> Optional[list[dict]]:
    if not tools:
        return None
    if api_mode != "responses":
        return tools
    out = []
    for t in tools:
        fn = t.get("function", t)
        out.append({
            "type": "function",
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


class LLMClient:
    """One endpoint. Use chat_stream(); it yields text/reasoning deltas and
    returns a ChatResult with accumulated tool_calls + usage."""

    def __init__(self, s: LLMSettings, *, client: Optional[httpx.AsyncClient] = None):
        self.s = s
        self._client = client
        self._own_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.s.timeout, connect=15.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._client

    async def aclose(self) -> None:
        if self._own_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    def _build_request(self, messages: list[dict], tools: Optional[list[dict]],
                       *, max_tokens: Optional[int] = None):
        s = self.s
        headers = {"Authorization": f"Bearer {s.api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"}
        if s.api_mode == "responses":
            url = _auto_url(s.base_url, "responses")
            payload: dict[str, Any] = {
                "model": s.model,
                "input": _to_responses_input(messages),
                "stream": True,
                "prompt_cache_key": _RESP_CACHE_KEY,
                "instructions": s.system_prompt or "You are a helpful assistant.",
            }
            if s.reasoning_effort:
                payload["reasoning"] = {"effort": s.reasoning_effort}
            if s.max_tokens:
                payload["max_output_tokens"] = s.max_tokens
        else:
            url = _auto_url(s.base_url, "chat/completions")
            payload = {
                "model": s.model,
                "messages": _to_chat_messages(messages, s.system_prompt),
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if s.temperature != 1:
                payload["temperature"] = s.temperature
            if s.max_tokens:
                ml = (s.model or "").lower()
                key = "max_completion_tokens" if ml.startswith(("gpt-5", "o1", "o2", "o3", "o4")) else "max_tokens"
                payload[key] = s.max_tokens
            if s.reasoning_effort:
                payload["reasoning_effort"] = s.reasoning_effort
        pt = _prepare_tools(tools, s.api_mode)
        if pt:
            payload["tools"] = pt
        # Per-call output cap (e.g. compaction summaries must stay small).
        if max_tokens:
            if s.api_mode == "responses":
                payload["max_output_tokens"] = max_tokens
            else:
                ml = (s.model or "").lower()
                key = "max_completion_tokens" if ml.startswith(("gpt-5", "o1", "o2", "o3", "o4")) else "max_tokens"
                payload[key] = max_tokens
        return url, headers, payload


    async def run(self, messages: list[dict], tools: Optional[list[dict]] = None,
                  on_delta=None, stop_check=None, *,
                  max_tokens: Optional[int] = None) -> ChatResult:
        """Run one non-streaming-collect call. Streams deltas to on_delta(kind, text)
        where kind is 'text' or 'reasoning'. Returns ChatResult.

        ``max_tokens`` overrides the configured output cap for this call only.
        """
        url, headers, payload = self._build_request(messages, tools, max_tokens=max_tokens)
        client = self._get_client()

        attempt = 0
        while True:
            content = ""
            reasoning = ""
            tc_buf: dict = {}
            usage: Optional[dict] = None
            got_any = False
            try:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        msg = f"HTTP {resp.status_code}: {body[:500]}"
                        retryable = resp.status_code in (408, 409, 425, 429, 500, 502, 503, 504, 529)
                        if retryable and attempt < self.s.max_retries and not got_any:
                            attempt += 1
                            continue
                        raise LLMError(msg)
                    async for line in resp.aiter_lines():
                        if stop_check and stop_check():
                            break
                        data = _sse_payload(line)
                        if data is None:
                            continue
                        if data == "[DONE]":
                            break
                        try:
                            evt = json.loads(data)
                        except Exception:
                            continue
                        perr = _provider_error(evt)
                        if perr:
                            raise LLMError(f"provider error: {perr}")
                        if self.s.api_mode == "responses":
                            etype = evt.get("type", "")
                            if etype == "response.output_text.delta":
                                d = evt.get("delta", "")
                                if d:
                                    content += d; got_any = True
                                    if on_delta: on_delta("text", d)
                            elif etype == "response.reasoning_summary_text.delta":
                                d = evt.get("delta", "")
                                if d:
                                    reasoning += d; got_any = True
                                    if on_delta: on_delta("reasoning", d)
                            elif etype == "response.output_item.added":
                                item = evt.get("item", {})
                                if item.get("type") == "function_call":
                                    i2 = evt.get("output_index", 0)
                                    tc_buf[i2] = {"id": item.get("call_id", item.get("id", "")), "name": item.get("name", ""), "args": ""}
                            elif etype == "response.function_call_arguments.delta":
                                i2 = evt.get("output_index", 0)
                                if i2 in tc_buf:
                                    tc_buf[i2]["args"] += evt.get("delta", "")
                            elif etype in ("response.failed", "response.incomplete"):
                                r = evt.get("response") or {}
                                e = r.get("error") or {}
                                emsg = (e.get("message") if isinstance(e, dict) else None) or (r.get("incomplete_details") or {}).get("reason") or etype
                                raise LLMError(f"provider {etype}: {emsg}")
                            elif etype == "response.completed":
                                usage = _normalize_usage((evt.get("response") or {}).get("usage") or {}, "responses")
                        else:
                            ch = (evt.get("choices") or [{}])[0]
                            delta = ch.get("delta") or {}
                            dr = delta.get("reasoning_content") or delta.get("reasoning")
                            if dr:
                                reasoning += dr; got_any = True
                                if on_delta: on_delta("reasoning", dr)
                            if delta.get("content"):
                                content += delta["content"]; got_any = True
                                if on_delta: on_delta("text", delta["content"])
                            for tc in (delta.get("tool_calls") or []):
                                i2 = tc.get("index", 0)
                                has_name = bool((tc.get("function") or {}).get("name"))
                                if i2 not in tc_buf:
                                    if has_name or not tc_buf:
                                        tc_buf[i2] = {"id": tc.get("id") or "", "name": "", "args": ""}
                                    else:
                                        i2 = max(tc_buf)
                                if has_name:
                                    tc_buf[i2]["name"] = tc["function"]["name"]
                                if (tc.get("function") or {}).get("arguments"):
                                    tc_buf[i2]["args"] += tc["function"]["arguments"]
                                if tc.get("id") and not tc_buf[i2]["id"]:
                                    tc_buf[i2]["id"] = tc["id"]
                            if evt.get("usage"):
                                usage = _normalize_usage(evt["usage"], "chat_completions")
                break
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as e:
                if attempt < self.s.max_retries and not got_any:
                    attempt += 1
                    continue
                raise LLMError(f"network error: {type(e).__name__}: {e}")

        tool_calls = []
        for i2 in sorted(tc_buf):
            tc = tc_buf[i2]
            parsed = _try_parse_args(tc["args"])
            for k, args in enumerate(parsed):
                cid = tc["id"] or f"call_{uuid.uuid4().hex[:8]}"
                if len(parsed) > 1:
                    cid = f"{cid}_{k}"
                tool_calls.append({"id": cid, "type": "function",
                                   "function": {"name": tc["name"], "arguments": json.dumps(args, ensure_ascii=False)}})
        return ChatResult(content=content, reasoning=reasoning, tool_calls=tool_calls, usage=usage)
