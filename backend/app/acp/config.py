"""ACP 客户端配置：只解析环境变量，不读写文件、不启动进程。

设计约束（见 docs/20261109-ACP-client驱动GenericAgent设计与实施.md §10）：

- 所有 ACP 常量集中在本模块，**不进** ``app/config.py``（ledger ruling R3）；
- 子进程环境只走白名单，绝不把 inner 的 ``INNER_*``、密钥、数据库路径传给 GA。
"""
from __future__ import annotations

import json
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass

# 只给解释器时自动补齐的 bridge 模块名。
GA_BRIDGE_MODULE = "frontends.acp.genericagent_acp_bridge"

# 子进程固定透传的系统变量：少一个都可能让 GA 起不来。
# APPDATA / LOCALAPPDATA 是硬要求——GA 用 %APPDATA%/GenericAgent 找 llm.yaml。
_SYSTEM_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "SystemRoot",
    "SystemDrive",
    "windir",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "LANG",
    "PYTHONIOENCODING",
)

# 存在才透传：代理出口 + GA 自身配置定位。
_OPTIONAL_ENV_KEYS: tuple[str, ...] = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "GA_CONFIG_DIR",
    "GA_LLM_CONFIG",
    "GA_MCP_CONFIG",
)


@dataclass(frozen=True)
class AcpSettings:
    """ACP 客户端的全部开关与限额，一次解析、处处只读。"""

    enabled: bool
    command: list[str]
    mode: str
    permission: str
    workdir_mode: str
    max_active_user: int
    max_active_global: int
    max_open_user: int
    prompt_timeout_s: float
    drain_timeout_s: float
    idle_ttl_s: float
    stderr_lines: int
    env_allow: tuple[str, ...]
    mcp_config: str
    llm_no: str = ""


def _text(env: Mapping[str, str], name: str, default: str = "") -> str:
    """取字符串变量；缺失或 strip 后为空都回落默认值。"""
    raw = env.get(name)
    if raw is None:
        return default
    stripped = raw.strip()
    return stripped or default


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _text(env, name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _text(env, name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _strip_quotes(token: str) -> str:
    """``shlex.split(posix=False)`` 会保留引号，去掉成对外层引号再交给 subprocess。"""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _parse_command(env: Mapping[str, str], llm_no: str = "") -> list[str]:
    """``INNER_ACP_GA_COMMAND`` 优先；为空时回落 ``-m`` 形式。

    JSON 数组形式以 ``[`` 开头，否则按 shlex 处理；两种形式解析失败都按空处理
    （即不启用），绝不把解析异常抛给启动路径。

    ``llm_no`` 非空时追加 ``--llm-no <n>``；显式命令里已经带了就不再追加。
    """
    raw = _text(env, "INNER_ACP_GA_COMMAND")
    command: list[str]
    if raw:
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if not isinstance(parsed, list):
                return []
            command = [str(item) for item in parsed if str(item)]
        else:
            try:
                tokens = shlex.split(raw, posix=False)
            except ValueError:
                return []
            command = [t for t in (_strip_quotes(t) for t in tokens) if t]
    else:
        python = _text(env, "INNER_ACP_GA_PYTHON")
        command = [python, "-m", GA_BRIDGE_MODULE] if python else []

    if command and llm_no and not _has_llm_no(command):
        command = command + ["--llm-no", llm_no]
    return command


def _has_llm_no(command: list[str]) -> bool:
    return any(token == "--llm-no" or token.startswith("--llm-no=") for token in command)


def _parse_env_allow(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = _text(env, "INNER_ACP_ENV_ALLOW")
    if not raw:
        return ()
    return tuple(item for item in (part.strip() for part in raw.split(",")) if item)


def load_acp_settings(env: Mapping[str, str] | None = None) -> AcpSettings:
    """从环境变量解析 ACP 设置；``env`` 缺省为 ``os.environ``。"""
    e = os.environ if env is None else env
    llm_no = _text(e, "INNER_ACP_GA_LLM_NO")
    command = _parse_command(e, llm_no)
    return AcpSettings(
        enabled=bool(command),
        command=command,
        mode=_text(e, "INNER_ACP_MODE", "full_access"),
        permission=_text(e, "INNER_ACP_PERMISSION", "allow"),
        workdir_mode=_text(e, "INNER_ACP_WORKDIR_MODE", "workspace"),
        max_active_user=_int(e, "INNER_ACP_MAX_ACTIVE_USER", 4),
        max_active_global=_int(e, "INNER_ACP_MAX_ACTIVE_GLOBAL", 80),
        max_open_user=_int(e, "INNER_ACP_MAX_OPEN_USER", 8),
        prompt_timeout_s=_float(e, "INNER_ACP_PROMPT_TIMEOUT_S", 1800.0),
        drain_timeout_s=_float(e, "INNER_ACP_DRAIN_TIMEOUT_S", 300.0),
        idle_ttl_s=_float(e, "INNER_ACP_IDLE_TTL_S", 600.0),
        stderr_lines=_int(e, "INNER_ACP_STDERR_LINES", 200),
        env_allow=_parse_env_allow(e),
        mcp_config=_text(e, "INNER_ACP_GA_MCP_CONFIG"),
        llm_no=llm_no,
    )


def build_child_env(
    settings: AcpSettings, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """构造 GA 子进程环境：固定白名单 + 可选透传 + 显式白名单 + MCP 覆盖。

    只从 ``base``（缺省 ``os.environ``）取值；**不含任何其它 ``INNER_*``**。
    """
    src = os.environ if base is None else base
    out: dict[str, str] = {}
    for key in _SYSTEM_ENV_KEYS + _OPTIONAL_ENV_KEYS + settings.env_allow:
        value = src.get(key)
        if value is not None:
            out[key] = value
    if settings.mcp_config:
        out["GA_MCP_CONFIG"] = settings.mcp_config
    return out
