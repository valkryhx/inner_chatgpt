"""子代理提示词应包含的约束（TDD 红先）。"""
from __future__ import annotations


def _hint() -> str:
    from app.subagent.prompts import SUBAGENT_HINT

    return SUBAGENT_HINT


def test_requires_verification_before_claiming_done():
    h = _hint()
    assert "验证" in h
    assert "证据" in h


def test_prefers_dedicated_search_tools():
    h = _hint()
    assert "file_glob" in h and "file_grep" in h


def test_final_answer_starts_with_conclusion():
    h = _hint()
    assert "第一行" in h or "结论" in h


def test_keeps_existing_boundaries():
    h = _hint()
    assert "不要扩大范围" in h
    assert "frontend/dist" in h


def test_teaches_nonblocking_wait_semantics():
    """wait_agent 默认不等完；提示词必须把三态讲清楚，否则模型会空等。"""
    from app.subagent.prompts import ROOT_AGENT_HINT

    h = ROOT_AGENT_HINT
    assert "state=event" in h
    assert "state=all_terminal" in h
    assert "state=timeout" in h or "timeout" in h
    assert "condition=all_terminal" in h
