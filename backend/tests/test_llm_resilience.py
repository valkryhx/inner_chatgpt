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

# ---------------- 连接池配置 ----------------

def test_pool_timeout_is_bounded_not_read_timeout():
    """池满时不能等满 read_timeout。

    缺陷形态（2026-11-12 实测 + 真机日志）：httpx.Timeout(read, connect=…)
    没给 pool 时，**pool 等待沿用 read 超时**，于是连接池占满后第 N+1 个请求
    干等 120s 才抛 PoolTimeout。真机日志里连续十几条 PoolTimeout，相邻重试
    间隔正好 2 分钟。
    """
    c = LLMClient(_settings(timeout=120))
    try:
        t = c._get_client().timeout
        assert t.read == 120, t
        assert t.pool is not None, "pool 必须显式设置，否则会继承 read 超时"
        assert t.pool <= 30, f"池等待上限过大：{t.pool}s"
    finally:
        asyncio.run(c.aclose())


def test_connection_limit_above_default_subagent_count():
    """池上限必须大于 USER_MAX_ACTIVE_SUBAGENTS(8)，否则主轮+teammate 必然排队。"""
    from app.llm_client import _max_connections_from_env

    assert _max_connections_from_env() > 8


def test_connection_limit_env_override(monkeypatch):
    from app.llm_client import _max_connections_from_env

    monkeypatch.setenv("HLZ_LLM_MAX_CONNECTIONS", "64")
    assert _max_connections_from_env() == 64
    monkeypatch.setenv("HLZ_LLM_MAX_CONNECTIONS", "bad")
    assert _max_connections_from_env() == 32
    monkeypatch.setenv("HLZ_LLM_MAX_CONNECTIONS", "0")
    assert _max_connections_from_env() >= 2, "0 会被 clamp 到下限，不能真的关掉连接"


# ---------------- 连接池自适应扩容 ----------------

def _client_with_pool(limit: int = 4, ceiling: int = 16) -> LLMClient:
    c = LLMClient(_settings())
    c._pool_limit = limit
    c._pool_ceiling = ceiling
    pool = c._get_client()._transport._pool
    pool._max_connections = limit
    return c


def test_grow_pool_doubles_until_ceiling():
    c = _client_with_pool(limit=4, ceiling=16)
    try:
        pool = c._get_client()._transport._pool
        assert c._grow_pool() == 8
        assert pool._max_connections == 8
        assert c._grow_pool() == 16
        assert pool._max_connections == 16
        # 到顶后不再涨
        assert c._grow_pool() == 16
        assert pool._max_connections == 16
    finally:
        asyncio.run(c.aclose())


def test_grow_pool_skips_when_not_owner():
    """注入的 client 共享父池，不能各自改上限（会互相打架）。"""
    owner = LLMClient(_settings())
    try:
        pool = owner._get_client()
        view = LLMClient(_settings(), client=pool)   # 非所有者
        before = pool._transport._pool._max_connections
        assert view._grow_pool() == view._pool_limit  # 返回原值，不扩容
        assert pool._transport._pool._max_connections == before
    finally:
        asyncio.run(owner.aclose())


def test_grow_pool_survives_private_api_breakage():
    """拿不到私有属性时静默放弃，绝不能让请求失败。"""
    c = _client_with_pool()

    class Broken:
        @property
        def _pool(self):
            raise AttributeError("httpcore 改了内部结构")
    try:
        c._client._transport = Broken()   # type: ignore[assignment]
        assert c._grow_pool() == c._pool_limit
    finally:
        c._client = None
        asyncio.run(c.aclose())


def test_pool_ceiling_is_bounded(monkeypatch):
    from app.llm_client import _pool_ceiling_from_env

    monkeypatch.setenv("HLZ_LLM_POOL_CEILING", "256")
    assert _pool_ceiling_from_env() == 256
    monkeypatch.setenv("HLZ_LLM_POOL_CEILING", "99999")
    assert _pool_ceiling_from_env() == 1024, "必须硬性 clamp，不能无限涨"
    monkeypatch.setenv("HLZ_LLM_POOL_CEILING", "0")
    assert _pool_ceiling_from_env() >= 2


def test_shrink_restores_initial_after_idle(monkeypatch):
    from app import llm_client as lc

    monkeypatch.setenv("HLZ_LLM_MAX_CONNECTIONS", "8")
    monkeypatch.setenv("HLZ_LLM_POOL_SHRINK_S", "60")
    c = LLMClient(_settings())
    try:
        pool = c._get_client()._transport._pool
        c._pool_limit = 8
        pool._max_connections = 8
        c._grow_pool()
        assert pool._max_connections == 16
        # 刚扩容不该立刻回落
        c._maybe_shrink_pool()
        assert pool._max_connections == 16
        # 假装已经空闲很久
        c._pool_grew_at = 0.0
        monkeypatch.setattr(lc.time, "monotonic", lambda: 10_000.0)
        c._maybe_shrink_pool()
        assert pool._max_connections == 8, "空闲后应回落到初始值"
    finally:
        asyncio.run(c.aclose())


def test_shrink_disabled_when_zero(monkeypatch):
    monkeypatch.setenv("HLZ_LLM_POOL_SHRINK_S", "0")
    from app.llm_client import _pool_idle_shrink_s
    assert _pool_idle_shrink_s() == 0.0


@pytest.mark.asyncio
async def test_pool_timeout_triggers_growth(monkeypatch, _no_sleep):
    """PoolTimeout 必须真的触发扩容 —— 这是唯一的饱和信号。

    反向确认时发现：只断言「并发请求最终成功」抓不住这个回归，
    因为客户端会重试，没有扩容也能成功（只是慢）。必须直接断言上调。
    """
    # max_retries=0：只发一次，断言才能精确到「一次 PoolTimeout 涨一档」。
    # 用 1 会让重试再撞一次池，连涨两档（4→8→16），断言就锁不住翻倍语义。
    c = LLMClient(_settings(max_retries=0))
    try:
        pool = c._get_client()._transport._pool
        c._pool_limit = 4
        c._pool_ceiling = 16
        pool._max_connections = 4
        grown: list[int] = []
        real = c._grow_pool

        def spy(**kw):
            new = real(**kw)
            grown.append(new)
            return new

        monkeypatch.setattr(c, "_grow_pool", spy)

        def boom(*a, **k):
            raise httpx.PoolTimeout("pool exhausted")

        monkeypatch.setattr(httpx.AsyncClient, "stream", boom)
        with pytest.raises(LLMError):
            await c.run([{"role": "user", "content": "hi"}])
        assert grown, "PoolTimeout 必须触发扩容"
        assert grown[0] == 8, f"应翻倍到 8，实际 {grown}"
        assert pool._max_connections == 8
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_non_pool_timeout_does_not_grow(monkeypatch, _no_sleep):
    """普通读超时（不是池满）不该扩容 —— 那是上游慢，不是池不够。"""
    c = LLMClient(_settings(max_retries=1))
    try:
        c._pool_limit = 4
        c._pool_ceiling = 16
        grown: list[int] = []
        monkeypatch.setattr(c, "_grow_pool", lambda **kw: grown.append(1) or 4)

        def boom(*a, **k):
            raise httpx.ReadTimeout("upstream slow")

        monkeypatch.setattr(httpx.AsyncClient, "stream", boom)
        with pytest.raises(LLMError):
            await c.run([{"role": "user", "content": "hi"}])
        assert not grown, "读超时不该触发扩容"
    finally:
        await c.aclose()


# ---------------- 读超时可配置 ----------------

def test_read_timeout_default_is_120():
    from app.llm import _read_timeout_from_config
    assert _read_timeout_from_config({}) == 120


def test_read_timeout_from_config_wins():
    from app.llm import _read_timeout_from_config
    assert _read_timeout_from_config({"read_timeout": 300}) == 300


def test_read_timeout_accepts_legacy_timeout_key():
    """兼容旧调用方用的 timeout 键名。"""
    from app.llm import _read_timeout_from_config
    assert _read_timeout_from_config({"timeout": 200}) == 200


def test_read_timeout_env_fallback(monkeypatch):
    from app.llm import _read_timeout_from_config
    monkeypatch.setenv("HLZ_LLM_READ_TIMEOUT", "240")
    assert _read_timeout_from_config({}) == 240


def test_read_timeout_config_beats_env(monkeypatch):
    from app.llm import _read_timeout_from_config
    monkeypatch.setenv("HLZ_LLM_READ_TIMEOUT", "240")
    assert _read_timeout_from_config({"read_timeout": 300}) == 300


def test_read_timeout_is_clamped():
    """下限防误配成 0（等于立即超时），上限防把坏端点当成「慢」。"""
    from app.llm import _read_timeout_from_config
    assert _read_timeout_from_config({"read_timeout": 1}) == 10
    assert _read_timeout_from_config({"read_timeout": 0}) == 120, "0 视为未设置"
    assert _read_timeout_from_config({"read_timeout": 99999}) == 600
    assert _read_timeout_from_config({"read_timeout": "bad"}) == 120


@pytest.mark.asyncio
async def test_read_timeout_reaches_the_http_client():
    """配置必须真的传到 httpx 的 timeout 上，否则这整条链路是摆设。"""
    from app.llm import build_client_from_config
    c = build_client_from_config({
        "model": "m", "base_url": "https://h/v1", "api_key": "k",
        "wire_api": "openai_chat", "read_timeout": 300,
    })
    try:
        assert c.s.timeout == 300
        assert c._get_client().timeout.read == 300
    finally:
        await c.aclose()
