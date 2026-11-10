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


# ---- 消融实验用的提示词变体开关（env 在调用时读取，不是 import 时冻结）----


def test_select_system_prompt_defaults_to_new(monkeypatch):
    from app.agent import SYSTEM_PROMPT, select_system_prompt

    monkeypatch.delenv("HLAUDZ_PROMPT_VARIANT", raising=False)
    assert select_system_prompt() == SYSTEM_PROMPT


def test_select_system_prompt_legacy(monkeypatch):
    from app.agent import SYSTEM_PROMPT_LEGACY, select_system_prompt

    monkeypatch.setenv("HLAUDZ_PROMPT_VARIANT", "legacy")
    assert select_system_prompt() == SYSTEM_PROMPT_LEGACY


def test_select_system_prompt_unknown_value_falls_back(monkeypatch):
    from app.agent import SYSTEM_PROMPT, select_system_prompt

    monkeypatch.setenv("HLAUDZ_PROMPT_VARIANT", "nonsense")
    assert select_system_prompt() == SYSTEM_PROMPT


def test_legacy_prompt_is_the_pre_change_text():
    from app.agent import SYSTEM_PROMPT_LEGACY

    # 旧版只有「工具使用与自纠」「本地工作区与 Skill」两节
    assert "工具使用与自纠" in SYSTEM_PROMPT_LEGACY
    assert "身份与边界" not in SYSTEM_PROMPT_LEGACY
    assert len(SYSTEM_PROMPT_LEGACY) < 1500


def test_compose_system_prompt_honours_env_at_call_time(monkeypatch):
    from app import agent as agent_mod

    monkeypatch.setenv("HLAUDZ_PROMPT_VARIANT", "legacy")
    legacy = agent_mod.compose_system_prompt("[SKILLS]")
    monkeypatch.delenv("HLAUDZ_PROMPT_VARIANT", raising=False)
    newer = agent_mod.compose_system_prompt("[SKILLS]")
    assert "工具使用与自纠" in legacy
    assert "身份与边界" not in legacy
    assert "身份与边界" in newer
    # skill 索引与 root 编排提示两块都要拼进去
    assert "[SKILLS]" in legacy and "[SKILLS]" in newer
