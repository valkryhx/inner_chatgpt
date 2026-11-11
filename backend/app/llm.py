"""LLM config -> runtime client (no langchain).

`LLMSettings` (defined in llm_client) is the flat runtime config. This module
only turns yaml/db config into an LLMSettings and builds a `LLMClient`.
"""
from __future__ import annotations

from .config import LLMSettings, load_llm_settings  # noqa: F401 (re-export)
from .llm_client import LLMClient, LLMSettings as ClientSettings


def _from_settings(s: LLMSettings, *, max_tokens: int | None = None, system_prompt: str = "") -> LLMClient:
    return LLMClient(ClientSettings(
        model=s.model,
        base_url=s.base_url,
        api_key=s.api_key,
        wire_api=s.wire_api,
        temperature=s.temperature,
        max_tokens=(max_tokens if max_tokens is not None else s.max_tokens),
        timeout=s.timeout,
        max_retries=s.max_retries,
        reasoning_effort=s.reasoning_effort,
        system_prompt=system_prompt,
    ))


def build_client(settings: LLMSettings | None = None, *, max_tokens: int | None = None,
                 system_prompt: str = "") -> LLMClient:
    s = settings or load_llm_settings()
    return _from_settings(s, max_tokens=max_tokens, system_prompt=system_prompt)


def build_client_from_config(cfg: dict, *, max_tokens: int | None = None,
                             system_prompt: str = "") -> LLMClient:
    """Build an LLMClient from a user-level LLM config dict.

    cfg keys: wire_api / base_url / api_key / model / max_tokens / reasoning_effort / ...
    """
    from .llm_client import max_retries_from_env

    wire = (cfg.get("wire_api") or "openai_chat").strip()
    tokens = max_tokens if max_tokens is not None else int(cfg.get("max_tokens", 8192) or 8192)
    # 重试预算：用户配置 > 环境变量 > 默认。
    # 用户配置表里根本没有 max_retries 字段（见 user_llm_configs），所以以前的
    # 硬编码 2 会让 HLZ_LLM_MAX_RETRIES 完全失效——真机 502 时只重试 2 次就放弃，
    # 而运维以为自己已经调到 4 了。
    raw_retries = cfg.get("max_retries")
    if raw_retries is None or str(raw_retries).strip() == "":
        retries = max_retries_from_env()
    else:
        try:
            retries = max(0, min(int(raw_retries), 8))
        except (TypeError, ValueError):
            retries = max_retries_from_env()
    return LLMClient(ClientSettings(
        model=cfg.get("model", ""),
        base_url=cfg.get("base_url", ""),
        api_key=cfg.get("api_key", ""),
        wire_api=wire,
        temperature=float(cfg.get("temperature", 1.0) or 1.0),
        max_tokens=tokens,
        timeout=int(cfg.get("timeout", 120) or 120),
        max_retries=retries,
        reasoning_effort=(cfg.get("reasoning_effort") or None),
        system_prompt=system_prompt,
    ))


def clone_client_with_effort(client: LLMClient, effort: str) -> LLMClient:
    """同一端点、同一连接池，只换思考等级。

    共享底层 httpx.AsyncClient（连接池），避免每次改等级都新建连接；
    新 client 不 own 该连接，所以它的 aclose() 不会把共用的池关掉。
    """
    from .reasoning import apply_effort

    settings = apply_effort(client.s, effort)
    if settings is client.s:
        return client
    return LLMClient(settings, client=client._client)
