"""思考等级（reasoning_effort）的合法值与归一化。

可选值故意比任何单一模型支持的范围都宽：不同 provider 认的子集不一样
（有的只认 low/medium/high，有的认 minimal，Anthropic 系认 max，xhigh/ultra 是
部分新版端点才有的）。放在这里统一校验，**能不能用由上游决定**——真不匹配时
由 provider 返回错误，我们把模型名和等级一起透出（见 llm_client 的错误包装）。

空字符串 = 不覆盖，沿用模型配置里的值（或上游默认）。
"""
from __future__ import annotations

from typing import Optional

REASONING_LEVELS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)


class InvalidReasoningEffort(ValueError):
    """传了不在枚举里的等级。消息里必须能看出传了什么、合法值有哪些。"""


def normalize_effort(value: Optional[str]) -> str:
    """归一化：去空白、转小写；空表示「不覆盖」；非法值抛可读错误。"""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if raw not in REASONING_LEVELS:
        allowed = " / ".join(REASONING_LEVELS)
        raise InvalidReasoningEffort(
            f"不支持的思考等级 {value!r}；可选值：{allowed}（留空表示沿用模型配置）"
        )
    return raw


def apply_effort(settings, override: Optional[str]):
    """按会话级覆盖克隆一份 settings。

    runtime 是按 (user, model) 缓存的，**绝不能改原对象**——否则 A 会话选的等级
    会漏到 B 会话。空覆盖直接原样返回，调用方不必分支。
    """
    import dataclasses

    level = normalize_effort(override)
    if not level:
        return settings
    return dataclasses.replace(settings, reasoning_effort=level)
