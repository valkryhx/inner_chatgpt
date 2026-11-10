"""思考等级（reasoning_effort）：可选值、会话级覆盖、明确的报错。"""
from __future__ import annotations

import pytest


def test_accepts_all_documented_levels():
    from app.reasoning import REASONING_LEVELS, normalize_effort

    for level in ("none", "minimal", "low", "high", "xhigh", "max", "ultra"):
        assert level in REASONING_LEVELS
        assert normalize_effort(level) == level


def test_empty_means_inherit():
    from app.reasoning import normalize_effort

    assert normalize_effort("") == ""
    assert normalize_effort(None) == ""
    assert normalize_effort("   ") == ""


def test_rejects_unknown_level_with_actionable_message():
    from app.reasoning import InvalidReasoningEffort, normalize_effort

    with pytest.raises(InvalidReasoningEffort) as exc:
        normalize_effort("turbo")
    msg = str(exc.value)
    # 报错要能看到「你传了什么」和「合法值有哪些」
    assert "turbo" in msg
    for level in ("none", "minimal", "low", "high", "xhigh", "max", "ultra"):
        assert level in msg


def test_case_and_space_insensitive():
    from app.reasoning import normalize_effort

    assert normalize_effort("HIGH") == "high"
    assert normalize_effort(" xhigh ") == "xhigh"


def test_session_override_beats_model_config():
    """会话级选择必须压过模型配置里的值；空值表示沿用配置。"""
    from dataclasses import replace

    from app.llm_client import LLMSettings
    from app.reasoning import apply_effort

    base = LLMSettings(model="m", base_url="http://x", api_key="k",
                       reasoning_effort="low", system_prompt="sys")
    got = apply_effort(base, "high")
    assert got.reasoning_effort == "high"
    # 原对象不能被改：runtime 是按 (user, model) 缓存的，改它会污染别的会话
    assert base.reasoning_effort == "low"
    # 空覆盖 = 原样返回
    assert apply_effort(base, "").reasoning_effort == "low"


def test_apply_effort_keeps_everything_else_identical():
    from app.llm_client import LLMSettings
    from app.reasoning import apply_effort

    base = LLMSettings(model="m", base_url="http://x", api_key="k",
                       system_prompt="sys", max_tokens=4096)
    got = apply_effort(base, "xhigh")
    assert got.model == base.model and got.base_url == base.base_url
    assert got.api_key == base.api_key and got.system_prompt == base.system_prompt
    assert got.max_tokens == base.max_tokens


def test_with_effort_does_not_mutate_cached_runtime():
    """会话覆盖必须派生新对象：缓存的是 (user, model) 级 runtime。"""
    from app.agent import with_effort
    from app.llm_client import LLMClient, LLMSettings

    class Model:
        model_name = "m"

    class Runtime:
        pass

    base = Runtime()
    m = Model()
    m.client = LLMClient(LLMSettings(model="m", base_url="http://x", api_key="k",
                                     reasoning_effort="low"))
    base.model = m
    base.user_id = "u1"
    base.registry = "reg"

    out = with_effort(base, "ultra")
    assert out is not base
    assert out.model.client.s.reasoning_effort == "ultra"
    assert base.model.client.s.reasoning_effort == "low"      # 原对象没被动
    assert out.registry is base.registry                       # 其余字段共享
    assert with_effort(base, "") is base                       # 空覆盖不复制


def test_unsupported_effort_error_names_model_and_level(monkeypatch):
    """上游 400 时，报错必须点明「哪个模型 + 哪个档」，否则用户看不懂。"""
    import asyncio

    import httpx

    from app.llm_client import LLMClient, LLMError, LLMSettings

    settings = LLMSettings(model="moma/deepseek-latest-flash", base_url="http://x",
                           api_key="k", reasoning_effort="ultra", max_retries=0)

    class Resp:
        status_code = 400

        async def aread(self):
            return b'{"error":"invalid reasoning_effort"}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Client:
        def stream(self, *a, **k):
            return Resp()

    c = LLMClient(settings, client=Client())

    async def go():
        with pytest.raises(LLMError) as exc:
            await c.run([{"role": "user", "content": "hi"}])
        return str(exc.value)

    msg = asyncio.run(go())
    assert "moma/deepseek-latest-flash" in msg
    assert "ultra" in msg
    assert "思考等级" in msg


def test_session_out_exposes_effort():
    """PATCH 之后必须能读回来，否则前端下拉显示不出当前档位。"""
    from app.schemas import SessionOut

    fields = SessionOut.model_fields
    assert "reasoning_effort" in fields
