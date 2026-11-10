"""SYSTEM_PROMPT 的行为约束（TDD 红先）：只测可执行、可观测的条目。"""
from __future__ import annotations


def _prompt() -> str:
    from app.agent import SYSTEM_PROMPT

    return SYSTEM_PROMPT


def test_has_tool_preference_section():
    p = _prompt()
    assert "file_glob" in p and "file_grep" in p
    assert "不要用 code_run" in p


def test_has_verification_discipline():
    p = _prompt()
    assert "验证" in p
    assert "证据" in p


def test_has_skill_trigger_mapping():
    p = _prompt()
    assert "任务类型" in p
    assert "load_skill" in p


def test_keeps_artifact_path_redline():
    p = _prompt()
    assert "frontend/dist" in p
    assert "workspace" in p


def test_keeps_markdown_image_rule():
    p = _prompt()
    assert "Markdown" in p


def test_length_is_bounded():
    # 提示词每轮都在上下文里：控制在 4500 字符以内
    assert len(_prompt()) <= 4500


def test_has_planning_and_delivery():
    p = _prompt()
    assert "计划" in p
    assert "结论" in p
