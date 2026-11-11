"""L1 韧性：模型调用层的自愈（退避重试 / Retry-After / 非流式降级 / 预算）。

背景（真机 runId 111915-64a6）：一次 LLM API 中断让 lead 僵死 927 秒，
任务 3 永久停在 pending。根因是旧实现「一旦流里出过任何 delta 就不再重试」，
断流后直接抛 LLMError，主循环随之结束。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import llm_client  # noqa: E402
from app.llm_client import LLMClient, LLMError, LLMSettings  # noqa: E402


def _settings(**kw) -> LLMSettings:
    base = dict(model="m", base_url="http://x/v1", api_key="k", max_retries=4)
    base.update(kw)
    return LLMSettings(**base)


def _sse(*chunks: str) -> bytes:
    out = []
    for c in chunks:
        out.append(f"data: {json.dumps(c if not isinstance(c, str) else {'choices': [{'delta': {'content': c}}]})}\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode()


def _sse_truncated(*chunks: str) -> bytes:
    """吐几个 delta 就断：没有 [DONE]、没有 finish_reason（上游连接被切）。"""
    return "".join(
        f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n\n"
        for c in chunks).encode()


def _mk(handler) -> LLMClient:
    return LLMClient(_settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """把退避等待压成 0，测试才跑得快；同时记录被要求等多久。"""
    waits: list[float] = []

    async def _fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr(llm_client.asyncio, "sleep", _fake_sleep)
    return waits


@pytest.mark.asyncio
async def test_retries_on_503_then_succeeds(_no_sleep):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, content=_sse("好了"))

    async with _mk(handler) as c:
        res = await c.run([{"role": "user", "content": "hi"}])

    assert calls["n"] == 2, "503 应重试一次"
    assert res.content == "好了"


@pytest.mark.asyncio
async def test_respects_retry_after_header(_no_sleep):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down", headers={"retry-after": "3"})
        return httpx.Response(200, content=_sse("ok"))

    async with _mk(handler) as c:
        await c.run([{"role": "user", "content": "hi"}])

    assert _no_sleep and _no_sleep[0] == 3.0, f"应听上游 Retry-After=3，实际 {_no_sleep}"


@pytest.mark.asyncio
async def test_never_retries_on_400(_no_sleep):
    """配置/参数错误重试一百次也不会好，只会白烧额度。"""
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    async with _mk(handler) as c:
        with pytest.raises(LLMError):
            await c.run([{"role": "user", "content": "hi"}])

    assert calls["n"] == 1, f"400 不该重试，实际请求 {calls['n']} 次"


@pytest.mark.asyncio
async def test_gives_up_after_max_retries(_no_sleep):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    async with _mk(handler) as c:
        with pytest.raises(LLMError):
            await c.run([{"role": "user", "content": "hi"}])

    assert calls["n"] == 5, f"max_retries=4 → 1 次初始 + 4 次重试，实际 {calls['n']}"


@pytest.mark.asyncio
async def test_stream_interrupted_after_deltas_falls_back_to_non_stream(_no_sleep):
    """核心回归：流里已经吐过内容后断流，必须降级非流式重试，而不是放弃。

    旧实现这里的 `not got_any` 直接 raise，正是 927s 僵死的根因。
    """
    calls: list[dict] = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        if len(calls) == 1:
            # 先吐 3 个 delta，然后断流
            return httpx.Response(200, content=_sse_truncated("一", "二", "三"))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "完整答案"}}],
        })

    async with _mk(handler) as c:
        seen: list[str] = []
        res = await c.run([{"role": "user", "content": "hi"}],
                          on_delta=lambda kind, t: seen.append(t))

    assert len(calls) >= 2, "断流后必须再试一次"
    assert calls[1].get("stream") is False, f"重试必须降级为非流式，实际 {calls[1].get('stream')}"
    assert res.content == "完整答案", f"应拿到完整结果，实际 {res.content!r}"


@pytest.mark.asyncio
async def test_non_stream_fallback_parses_tool_calls(_no_sleep):
    """降级路径也要能解析 tool_calls，否则工具调用会在重试后丢掉。"""
    calls: list[dict] = []

    def handler(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(200, content=_sse_truncated("半句"))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "tool_calls": [
                {"id": "call_1", "function": {"name": "file_read",
                                              "arguments": '{"path": "a.py"}'}}]}}],
        })

    async with _mk(handler) as c:
        res = await c.run([{"role": "user", "content": "hi"}])

    assert len(res.tool_calls) == 1, f"降级路径必须解析出工具调用，实际 {res.tool_calls}"
    assert res.tool_calls[0]["function"]["name"] == "file_read"


@pytest.mark.asyncio
async def test_retry_wait_budget_stops_hammering(monkeypatch, _no_sleep):
    """总等待预算耗尽就必须放弃，不能一直挂着（Web 多用户下等于挂死会话）。"""
    monkeypatch.setattr(llm_client, "RETRY_WAIT_BUDGET_S", 5.0)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429, text="slow", headers={"retry-after": "4"})

    async with _mk(handler) as c:
        with pytest.raises(LLMError) as ei:
            await c.run([{"role": "user", "content": "hi"}])

    assert "重试预算耗尽" in str(ei.value), f"应报预算耗尽，实际 {ei.value}"
    assert calls["n"] <= 3, f"预算 5s/每次 4s → 不该打很多次，实际 {calls['n']}"


@pytest.mark.asyncio
async def test_max_retries_env_is_clamped(monkeypatch):
    """误配成 100 会让坏上游把会话挂在那里反复重试，必须钳制。"""
    monkeypatch.setenv("HLZ_LLM_MAX_RETRIES", "100")
    assert llm_client.max_retries_from_env() == 8

    monkeypatch.setenv("HLZ_LLM_MAX_RETRIES", "0")
    assert llm_client.max_retries_from_env() == 0

    monkeypatch.setenv("HLZ_LLM_MAX_RETRIES", "abc")
    assert llm_client.max_retries_from_env() == 4, "非法值回落到默认"


@pytest.mark.asyncio
async def test_stop_check_breaks_stream(_no_sleep):
    """用户停止后不能继续读流（否则停止后还在消耗额度）。"""
    def handler(req):
        return httpx.Response(200, content=_sse("一", "二", "三", "四"))

    async with _mk(handler) as c:
        stop = {"v": False}

        def _on_delta(kind, t):
            if t == "二":
                stop["v"] = True

        res = await c.run([{"role": "user", "content": "hi"}],
                          on_delta=_on_delta, stop_check=lambda: stop["v"])

    assert "三" not in res.content, f"停止后不该继续累积，实际 {res.content!r}"


def test_build_client_from_config_honours_env(monkeypatch):
    """真机缺陷：用户模型配置表里没有 max_retries 字段，装配时被硬编码成 2，
    使 HLZ_LLM_MAX_RETRIES 完全失效——运维以为调到 4 了，实际只重试 2 次。
    """
    from app.llm import build_client_from_config

    monkeypatch.setenv("HLZ_LLM_MAX_RETRIES", "5")
    c = build_client_from_config({"model": "m", "base_url": "b", "api_key": "k"})
    assert c.s.max_retries == 5, f"缺省时应读环境变量，实际 {c.s.max_retries}"


def test_build_client_from_config_explicit_zero(monkeypatch):
    """显式写 0 是「不重试」的有效选择，不能被当成空值回落到默认。"""
    from app.llm import build_client_from_config

    monkeypatch.setenv("HLZ_LLM_MAX_RETRIES", "5")
    c = build_client_from_config({"model": "m", "base_url": "b", "api_key": "k",
                                 "max_retries": 0})
    assert c.s.max_retries == 0, f"显式 0 必须生效，实际 {c.s.max_retries}"


def test_retries_from_config_path(monkeypatch):
    """yaml/配置装配路径同样不能被硬编码值盖掉。"""
    from app.config import _retries_from

    monkeypatch.delenv("HLZ_LLM_MAX_RETRIES", raising=False)
    assert _retries_from({}) == 4, "缺省应是 4（不是旧的硬编码 3）"
    assert _retries_from({"max_retries": 0}) == 0
    assert _retries_from({"max_retries": 99}) == 8, "上限 8"
    monkeypatch.setenv("HLZ_LLM_MAX_RETRIES", "6")
    assert _retries_from({}) == 6