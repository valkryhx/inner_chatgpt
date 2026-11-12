"""Self-built LLM client (no langchain). httpx async, two wires:
  - openai_chat      -> POST {base}/chat/completions
  - openai_responses -> POST {base}/responses
Streams text/reasoning deltas and returns normalized tool_calls + usage.
Design reference: GenericAgent genericagent/llm/core.py (_openai_stream/_parse_openai_sse).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_RESP_CACHE_KEY = str(uuid.uuid4())
REASONING_MARK = "@@reasoning@@"

# 上游说"过一会儿再来"的错误码。与 GA llm/core.py:658 的 _RETRYABLE 一致。
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
# 配置/权限/参数问题：重试一百次也不会好，反而白烧额度。
NEVER_RETRY_STATUS = frozenset({400, 401, 402, 403, 404, 405, 413, 422})

# 单次 run() 里重试等待的累计上限。Web 多用户下无限等待等于挂死一个会话。
# 取 180s 对齐 CC319 的时间窗常量思想（m0591:126130）。
RETRY_WAIT_BUDGET_S = 180.0
BACKOFF_BASE_S = 0.8
BACKOFF_CAP_S = 8.0


def max_retries_from_env(default: int = 4) -> int:
    """重试预算：默认 4，可用 HLZ_LLM_MAX_RETRIES 覆盖，硬上限 8。

    上限是必须的：误配成 100 会让一个坏掉的上游把整个会话挂在那里反复重试。
    """
    raw = (os.environ.get("HLZ_LLM_MAX_RETRIES") or "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    return max(0, min(n, 8))


def _retry_after_s(resp: httpx.Response) -> Optional[float]:
    """上游给的 Retry-After（秒）。优先听它的，别自作聪明。"""
    raw = (resp.headers or {}).get("retry-after", "")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:  # 也接受 HTTP-date，但只用常见的秒数形式，避免引入解析依赖
        from email.utils import parsedate_to_datetime
        import datetime as _dt
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        delta = (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def _backoff_s(attempt: int, retry_after: Optional[float] = None) -> float:
    """指数退避 + 抖动；有 Retry-After 就听上游的。"""
    if retry_after is not None:
        return min(retry_after, BACKOFF_CAP_S * 4)
    base = min(BACKOFF_BASE_S * (2 ** max(0, attempt)), BACKOFF_CAP_S)
    return base * (0.75 + random.random() * 0.5)


class LLMError(Exception):
    """Model request failed (provider error / HTTP failure)."""


def _llm_retry_event(event: str, attempt: int, msg: str, *,
                     waited: float = 0.0, reason: str = "") -> None:
    """把重试写进团队事件流，便于 `tools/team_timeline.py` 复盘 API 中断。

    重试**不写进对话历史**（GA 的做法）：它是瞬时状态，写进 history 会让模型
    以为自己说错过什么。前端/日志能看见就够了。
    """
    try:
        from .logging_setup import team_event
        team_event(event, attempt=attempt, waited=round(waited, 2),
                   reason=reason or "", error=str(msg or "")[:300])
    except Exception:
        pass


def _collect_chat_tool_calls_delta(tcs: list[dict], tc_buf: dict) -> None:
    """累积流式 chat/completions 的 tool_calls 分片。"""
    for tc in tcs:
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


def _collect_chat_tool_calls(tcs: list[dict], tc_buf: dict) -> None:
    """非流式 chat/completions 的 tool_calls（一次性完整给出）。"""
    for i, tc in enumerate(tcs):
        fn = tc.get("function") or {}
        tc_buf[i] = {"id": tc.get("id") or "", "name": fn.get("name") or "",
                     "args": fn.get("arguments") or ""}


def _responses_text(evt: dict) -> str:
    """非流式 responses 的正文：拼接 output 里的 output_text 分片。"""
    parts: list[str] = []
    for item in (evt.get("output") or []):
        for c in (item.get("content") or []):
            if c.get("type") in ("output_text", "text") and c.get("text"):
                parts.append(str(c["text"]))
    return "".join(parts)


def _collect_responses_body(evt: dict, tc_buf: dict) -> None:
    """非流式 responses 的 function_call 项。"""
    for i, item in enumerate((evt.get("output") or [])):
        if item.get("type") == "function_call":
            tc_buf[i] = {"id": item.get("call_id") or item.get("id") or "",
                         "name": item.get("name") or "",
                         "args": item.get("arguments") or ""}


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
    max_retries: int = field(default_factory=max_retries_from_env)
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
        text = content if isinstance(content, str) else str(content or "")
        images = m.get("image_urls") if role == "user" else None
        if images:
            # 只有带图时 content 才变成数组；没图时逐字节保持原来的字符串形态。
            parts: list[dict[str, Any]] = []
            if text:
                parts.append({"type": "text", "text": text})
            for url in images:
                parts.append({"type": "image_url", "image_url": {"url": url}})
            item["content"] = parts
        else:
            item["content"] = text
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
        if role == "user":
            for url in (m.get("image_urls") or []):
                parts.append({"type": "input_image", "image_url": url})
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


def _max_connections_from_env(default: int = 32) -> int:
    """连接池初始上限。默认 32 而不是 8。

    8 是个危险的数字：`USER_MAX_ACTIVE_SUBAGENTS` 默认也是 8，主轮 + 8 个
    teammate 同时调模型时必然排队。排队本身不致命，但配合下面那个 pool
    超时缺陷就会变成「每次都要等 2 分钟」。

    注意这只是**起点**：真正在跑的时候会按需自动上调，见
    :meth:`LLMClient._grow_pool`。
    """
    raw = (os.environ.get("HLZ_LLM_MAX_CONNECTIONS") or "").strip()
    if not raw:
        return default
    try:
        return max(2, min(int(raw), 256))
    except (TypeError, ValueError):
        return default


def _pool_ceiling_from_env(default: int = 128) -> int:
    """自动扩容的硬上限。池可以涨，但不能无限涨。

    为什么必须有上限：连接不是免费的。每条 TCP/TLS 连接都占内核缓冲区与端口，
    上游网关通常也有每 key 的连接数限制。无限扩容的结果是把自己和上游一起打死。
    128 的依据：单用户最坏并发 = user_teams(4) 个 lead + user_subagents(8)
    + 重试余量，理论上个位数；128 留了一个数量级的安全余量，
    同时又远低于「一个进程打开上千连接」这种会引发真正的资源问题的量级。
    """
    raw = (os.environ.get("HLZ_LLM_POOL_CEILING") or "").strip()
    if not raw:
        return default
    try:
        return max(2, min(int(raw), 1024))
    except (TypeError, ValueError):
        return default


def _pool_idle_shrink_s() -> float:
    """池在多久没有池满信号之后回落到初始值。0 = 不回落。"""
    raw = (os.environ.get("HLZ_LLM_POOL_SHRINK_S") or "").strip()
    if not raw:
        return 600.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 600.0


class LLMClient:
    """One endpoint. Use chat_stream(); it yields text/reasoning deltas and
    returns a ChatResult with accumulated tool_calls + usage."""

    def __init__(self, s: LLMSettings, *, client: Optional[httpx.AsyncClient] = None):
        self.s = s
        self._client = client
        self._own_client = client is None
        # 连接池自适应扩容的当前上限。初值来自环境变量，池满时按需上调。
        # 只对「自己拥有的池」生效 —— 注入的 client（子代理复用父的、
        # clone_client_with_effort 派生视图）共享同一个池，由所有者负责扩容，
        # 否则多个 LLMClient 各自改同一个池的上限会互相打架。
        self._pool_limit = _max_connections_from_env()
        self._pool_ceiling = _pool_ceiling_from_env()
        self._pool_grew_at = 0.0

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # pool 单独设上限，原因是一个真实缺陷（2026-11-12 实测）：
            # httpx.Timeout(read, connect=…) 里没给 pool 时，**pool 等待会沿用
            # read 超时**。于是连接池被占满后，下一个请求不是快速失败，而是
            # 干等满 read_timeout（默认 120s）才抛 PoolTimeout。
            # 真机日志（backend/data/logs/app.log）里能看到这个形态：
            # 相邻两次重试间隔正好 2 分钟，连续十几条 PoolTimeout。
            #
            # 池上限 8 本身也不该小到与 USER_MAX_ACTIVE_SUBAGENTS(8) 相等 ——
            # 主轮 + 8 个 teammate 同时调用时必然排队。这里同时抬高上限。
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.s.timeout, connect=15.0, pool=10.0),
                limits=httpx.Limits(
                    max_connections=self._pool_limit,
                    max_keepalive_connections=8,
                ),
            )
        return self._client

    def _grow_pool(self, *, reason: str = "") -> int:
        """连接池满了：把上限翻倍，直到 ceiling。返回新的上限。

        **为什么是「按需扩容」而不是「一开始就设很大」**：
        连接真的会占资源（内核端口、TLS 握手、上游网关的每 key 限制）。
        多数用户的并发只有 1-3，给每个池都开 128 条纯属浪费。

        **为什么在 PoolTimeout 之后才扩**：这是唯一可靠的饱和信号。
        想看 `pool._requests` 排队数也行，但那是 httpcore 的私有字段，
        版本升级会变；而 PoolTimeout 是公开异常，语义稳定。

        **为什么翻倍而不是 +1**：池满通常意味着并发量级变了（比如突然派了
        一队 teammate），线性加 1 会连续撞墙好几次。翻倍 amortize 掉这些碰撞。

        实现上用 httpcore 的私有属性 `_max_connections`（httpx 没有公开 API）。
        这个属性在 httpcore 1.x 稳定，且改完立刻生效（已在 2026-11-12 实测：
        上限 3→9 后排队请求立刻被新一轮连接消化，9 个并发从 3 批变 1 批）。
        拿不到时静默放弃 —— 扩容是优化，绝不能因为改不了而让请求失败。
        """
        # 只有池的所有者能扩容；注入方（子代理 / effort 视图）共享同一个池。
        if not self._own_client or self._client is None:
            return self._pool_limit
        if self._pool_limit >= self._pool_ceiling:
            return self._pool_limit
        new_limit = min(self._pool_limit * 2, self._pool_ceiling)
        try:
            pool = self._client._transport._pool  # type: ignore[attr-defined]
            pool._max_connections = new_limit       # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 改不了就算了，不能影响请求
            logger.debug("连接池扩容失败（私有属性不可用），维持 %s", self._pool_limit)
            return self._pool_limit
        old = self._pool_limit
        self._pool_limit = new_limit
        self._pool_grew_at = time.monotonic()
        logger.warning("连接池已满，自动扩容 %s → %s（上限 %s）%s",
                       old, new_limit, self._pool_ceiling, f"：{reason}" if reason else "")
        _llm_retry_event("llm_pool_grow", 0, f"{old} -> {new_limit}", reason=reason)
        return new_limit

    def _maybe_shrink_pool(self) -> None:
        """长时间没有饱和就把上限收回去，避免一次高峰永久抬高水位。

        只改上限，不动已建连接 —— 空闲连接由 httpcore 自己的 keepalive
        过期逻辑回收，我们不需要插手。
        """
        idle_s = _pool_idle_shrink_s()
        if idle_s <= 0 or not self._own_client or self._client is None:
            return
        if self._pool_limit <= _max_connections_from_env():
            return
        if time.monotonic() - self._pool_grew_at < idle_s:
            return
        initial = _max_connections_from_env()
        try:
            pool = self._client._transport._pool  # type: ignore[attr-defined]
            pool._max_connections = initial         # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return
        logger.info("连接池空闲回落 %s → %s", self._pool_limit, initial)
        self._pool_limit = initial

    async def aclose(self) -> None:
        if self._own_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *exc) -> None:
        # 退出上下文时无论 client 是谁提供的都关掉：注入的 client 通常也是
        # 调用方为「这一批请求」临时建的，留着会泄漏连接。
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
        self._own_client = True

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
        budget_used = 0.0
        # 「流里已经吐过内容」后失败：不再原样重试（会把同一段文字吐两遍），
        # 而是降级成非流式再取一次完整结果（CC319 m0591:126373 retryWithoutStreaming）。
        degraded = False
        while True:
            content = ""
            reasoning = ""
            tc_buf: dict = {}
            usage: Optional[dict] = None
            got_any = False
            # 流式请求必须看到明确的收尾信号才算成功：[DONE] 或 finish_reason
            # （responses 用 response.completed）。都看不到就是断流。
            saw_done = False
            saw_finish = False
            stream = not degraded
            wait_s = 0.0
            fail_msg = ""
            try:
                async with client.stream("POST", url, headers=headers,
                                          json={**payload, "stream": stream} if not stream else payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        msg = f"HTTP {resp.status_code}: {body[:500]}"
                        # 思考等级不匹配（模型不支持该档）在上游一般是 400/422，
                        # 正文往往只有一句 invalid xxx。这里补上「哪个模型 + 哪个档」，
                        # 否则用户只会看到一段看不懂的上游错误。
                        if resp.status_code in (400, 422) and self.s.reasoning_effort:
                            msg += (
                                f"\n[思考等级不匹配] 模型 {self.s.model} 不接受 "
                                f"reasoning_effort={self.s.reasoning_effort!r}；"
                                "请在输入框下方的思考等级里换一档，或设为「不指定」。"
                            )
                        if resp.status_code in NEVER_RETRY_STATUS:
                            raise LLMError(msg)
                        if resp.status_code in RETRYABLE_STATUS:
                            fail_msg = msg
                            wait_s = _backoff_s(attempt, _retry_after_s(resp))
                        else:
                            raise LLMError(msg)
                        if not wait_s:
                            raise LLMError(fail_msg)
                    else:
                        if not stream:
                            # 非流式：一次读完整个 body
                            text = (await resp.aread()).decode("utf-8", "replace")
                            evt = json.loads(text) if text.strip() else {}
                            perr = _provider_error(evt)
                            if perr:
                                raise LLMError(f"provider error: {perr}")
                            if self.s.api_mode == "responses":
                                _collect_responses_body(evt, tc_buf)
                                content = _responses_text(evt)
                                usage = _normalize_usage((evt.get("response") or evt).get("usage") or {}, "responses")
                            else:
                                ch = (evt.get("choices") or [{}])[0]
                                msg0 = ch.get("message") or {}
                                content = msg0.get("content") or ""
                                reasoning = msg0.get("reasoning_content") or msg0.get("reasoning") or ""
                                _collect_chat_tool_calls(msg0.get("tool_calls") or [], tc_buf)
                                usage = _normalize_usage(evt.get("usage") or {}, "chat_completions")
                            got_any = bool(content or reasoning or tc_buf)
                            # 整包 JSON 读到了就是完整结束，没有「断流」概念
                            saw_done = saw_finish = True
                            if on_delta:
                                if reasoning:
                                    on_delta("reasoning", reasoning)
                                if content:
                                    on_delta("text", content)
                        else:
                            async for line in resp.aiter_lines():
                                if stop_check and stop_check():
                                    saw_done = True  # 用户主动停，不算断流
                                    break
                                data = _sse_payload(line)
                                if data is None:
                                    continue
                                if data == "[DONE]":
                                    saw_done = True
                                    break
                                try:
                                    evt = json.loads(data)
                                except Exception:
                                    continue
                                perr = _provider_error(evt)
                                if perr:
                                    fail_msg = f"provider error: {perr}"
                                    wait_s = _backoff_s(attempt)
                                    break
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
                                        fail_msg = f"provider {etype}: {emsg}"
                                        wait_s = _backoff_s(attempt)
                                        break
                                    elif etype == "response.completed":
                                        saw_finish = True
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
                                    _collect_chat_tool_calls_delta(delta.get("tool_calls") or [], tc_buf)
                                    if ch.get("finish_reason"):
                                        saw_finish = True
                                    if evt.get("usage"):
                                        usage = _normalize_usage(evt["usage"], "chat_completions")
                        if fail_msg:
                            # 流中途断掉/上游报错：下面统一决定重试方式
                            pass
                        elif not saw_done and not saw_finish:
                            # 上游把连接关了但既没发 [DONE] 也没发 finish_reason：
                            # 这是「断流」，不是「正常结束」。把它当成功会返回半截
                            # 内容，agent_loop 会以为模型答完了从而结束整轮——
                            # 真机 927s 僵死就是这种形态。降级非流式再取一次完整结果。
                            fail_msg = "stream ended without terminator (truncated)"
                            wait_s = 0.0
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError,
                    httpx.RemoteProtocolError, json.JSONDecodeError) as e:
                fail_msg = f"network error: {type(e).__name__}: {e}"
                wait_s = _backoff_s(attempt)
                # PoolTimeout 是唯一可靠的「池不够用」信号：说明请求在池里
                # 排到超时都没拿到连接。此时扩容，让这次重试就能用上新连接
                # （httpcore 立刻读 _max_connections，2026-11-12 实测）。
                if isinstance(e, httpx.PoolTimeout):
                    self._grow_pool(reason=f"pool timeout at attempt {attempt}")
            except httpx.HTTPError as e:
                fail_msg = f"network error: {type(e).__name__}: {e}"
                wait_s = _backoff_s(attempt)

            if not fail_msg:
                break

            # ---- 失败后的重试决策 ----
            # 1) 已经吐过内容：原样重试会重复内容 → 降级非流式取完整结果（只做一次）
            if got_any and stream and not degraded and attempt < self.s.max_retries:
                degraded = True
                attempt += 1
                logger.warning("LLM 流中断但有部分输出，降级为非流式重试（第 %s 次）：%s",
                               attempt, fail_msg[:200])
                _llm_retry_event("llm_retry_degraded", attempt, fail_msg, waited=0.0)
                continue
            # 2) 预算与等待闸
            if attempt >= self.s.max_retries:
                _llm_retry_event("llm_gave_up", attempt, fail_msg, waited=wait_s)
                raise LLMError(fail_msg)
            if budget_used + wait_s > RETRY_WAIT_BUDGET_S:
                _llm_retry_event("llm_gave_up", attempt, fail_msg, waited=wait_s,
                                 reason="retry_wait_budget_exhausted")
                raise LLMError(f"{fail_msg}\n[重试预算耗尽] 累计等待已超过 {int(RETRY_WAIT_BUDGET_S)}s")
            attempt += 1
            logger.warning("LLM 请求失败，%.1fs 后重试（第 %s/%s 次）：%s",
                           wait_s, attempt, self.s.max_retries, fail_msg[:200])
            _llm_retry_event("llm_retry", attempt, fail_msg, waited=wait_s)
            if wait_s > 0:
                await asyncio.sleep(wait_s)
                budget_used += wait_s
            continue

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
        # 成功返回时顺便看一眼是不是该把抬高过的上限收回去。
        # 放在这里而不是加定时器：调用成功就是天然的「一次检查机会」，
        # 不需要额外后台任务，也不会在完全空闲的进程里空转。
        self._maybe_shrink_pool()
        return ChatResult(content=content, reasoning=reasoning, tool_calls=tool_calls, usage=usage)
