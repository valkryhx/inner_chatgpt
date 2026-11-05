"""配置层：解析项目根目录 llm.yaml，输出可直接构造 LLMClient 的扁平设置。

解析链（对齐 GenericAgent llm/config.py 的思路，简化为本项目所需）：
    active_profile -> profiles[profile].model -> models[model].provider -> providers[provider]

llm.yaml 只读，绝不修改（含真实 key）。

注意：此处 LLMSettings 与 llm_client.LLMSettings 字段基本一致，
llm.py 负责显式映射；后续可合并为单一类型。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# backend/app/config.py -> parents[2] == 仓库根目录 inner_chatgpt/
ROOT_DIR = Path(__file__).resolve().parents[2]
LLM_YAML = ROOT_DIR / "llm.yaml"
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_PATH = DATA_DIR / "app.db"
# Per-user skill directories (SKILL.md + bundled files) and per-user workspaces
# (file read/write/patch, code_run working directory).
SKILLS_DIR = DATA_DIR / "skills"
WORKSPACES_DIR = DATA_DIR / "workspaces"
# Read-only skills shipped with the repository (available to every user).
BUILTIN_SKILLS_DIR = ROOT_DIR / "backend" / "skills_builtin"

_ENV_PAT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interp(value: Any) -> Any:
    """把字符串里的 ${ENV_VAR} 替换为环境变量（缺失则保留原样）。"""
    if not isinstance(value, str):
        return value

    def repl(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), m.group(0))

    return _ENV_PAT.sub(repl, value)


@dataclass
class LLMSettings:
    """一次模型接入所需的全部运行参数（扁平化）。"""

    model: str
    base_url: str
    api_key: str
    wire_api: str
    context_window: int
    soft_limit_tokens: int
    hard_limit_tokens: int
    temperature: float = 1.0
    max_tokens: int = 8192
    timeout: int = 120
    max_retries: int = 3
    reasoning_effort: str | None = None

    @property
    def use_responses_api(self) -> bool:
        return self.wire_api == "openai_responses"


def llm_yaml_available() -> bool:
    """仓库内 llm.yaml 是否存在。

    本项目定位为多用户 Web Agent：每个用户在自己的「设置 -> 我的模型」里配置
    base_url / api_key / model，因此 llm.yaml **不是必需文件**，也不入库。
    它仅作为本机可选的兜底 profile 存在。
    """
    return LLM_YAML.exists()


def _load_yaml() -> dict[str, Any]:
    if not LLM_YAML.exists():
        raise FileNotFoundError(
            "未找到 llm.yaml（可选兜底配置），且当前用户也没有可用的模型配置。"
            "请在页面「设置 -> 我的模型」里新增一个模型（base_url / api_key / model），"
            "并设为默认后再试。"
        )
    with open(LLM_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_llm_settings(profile: str | None = None) -> LLMSettings:
    """按 llm.yaml 解析出 LLMSettings。profile 默认取 active_profile。"""
    cfg = _load_yaml()
    profile = profile or cfg.get("active_profile") or "default"
    profiles = cfg.get("profiles") or {}
    if profile not in profiles:
        raise KeyError(f"profile not found in llm.yaml: {profile}")

    model_key = profiles[profile]["model"]
    models = cfg.get("models") or {}
    if model_key not in models:
        raise KeyError(f"model not found: {model_key}")
    m = models[model_key]

    provider_key = m.get("provider")
    providers = cfg.get("providers") or {}
    if provider_key not in providers:
        raise KeyError(f"provider not found: {provider_key}")
    p = providers[provider_key]

    defaults = cfg.get("defaults") or {}
    context_window = int(m.get("context_window") or 128000)
    soft = m.get("auto_compact_tokens")
    hard = m.get("hard_limit_tokens")
    if soft is None:
        soft = round(context_window * float(m.get("auto_compact_ratio", 0.90)))
    if hard is None:
        hard = round(context_window * float(m.get("hard_limit_ratio", 0.95)))

    return LLMSettings(
        model=str(m.get("api_model") or model_key),
        base_url=str(_interp(p.get("base_url") or "")),
        api_key=str(_interp(p.get("api_key") or "")),
        wire_api=str(p.get("wire_api") or "openai_chat"),
        context_window=context_window,
        soft_limit_tokens=int(soft),
        hard_limit_tokens=int(hard),
        temperature=float(m.get("temperature", defaults.get("temperature", 1.0))),
        max_tokens=int(m.get("max_tokens", defaults.get("max_tokens", 8192))),
        timeout=int(m.get("read_timeout", 120)),
        max_retries=int(m.get("max_retries", 3)),
        reasoning_effort=m.get("reasoning_effort"),
    )
