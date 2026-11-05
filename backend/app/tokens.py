"""Token 估计（CJK-aware）与消息计数。独立小模块，避免循环依赖。"""
from __future__ import annotations

from typing import Any, Iterable

# 摘要消息的标记（本项目 compaction.py 写入该 additional_kwargs，用于识别摘要消息）
SUMMARY_SOURCE_KEY = "lc_source"
SUMMARY_SOURCE_VAL = "summarization"


def estimate_tokens(text: str) -> int:
    """粗估 token：CJK 字符按 ~1 token，其它按 ~0.3 token/字符。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk + other * 0.3) + 1


def message_text(m: Any) -> str:
    # support both dict messages (self-built) and objects
    if isinstance(m, dict):
        c = m.get("content", "")
    else:
        c = getattr(m, "content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict):
                parts.append(str(b.get("text") or b.get("reasoning") or ""))
            else:
                parts.append(str(b))
        return " ".join(parts)
    return str(c)


def message_token_counter(messages: Iterable[Any]) -> int:
    """对话消息的 token 计数（CJK 感知，修正中文低估），供自研软线/硬线阈值判定使用。"""
    total = 0
    for m in messages:
        total += estimate_tokens(message_text(m))
    return total


def is_summary_message(m: Any) -> bool:
    if isinstance(m, dict):
        return m.get("_source") == SUMMARY_SOURCE_VAL
    ak = getattr(m, "additional_kwargs", None) or {}
    return ak.get(SUMMARY_SOURCE_KEY) == SUMMARY_SOURCE_VAL
