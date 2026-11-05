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
    wire = (cfg.get("wire_api") or "openai_chat").strip()
    tokens = max_tokens if max_tokens is not None else int(cfg.get("max_tokens", 8192) or 8192)
    return LLMClient(ClientSettings(
        model=cfg.get("model", ""),
        base_url=cfg.get("base_url", ""),
        api_key=cfg.get("api_key", ""),
        wire_api=wire,
        temperature=float(cfg.get("temperature", 1.0) or 1.0),
        max_tokens=tokens,
        timeout=int(cfg.get("timeout", 120) or 120),
        max_retries=int(cfg.get("max_retries", 2) or 2),
        reasoning_effort=(cfg.get("reasoning_effort") or None),
        system_prompt=system_prompt,
    ))
