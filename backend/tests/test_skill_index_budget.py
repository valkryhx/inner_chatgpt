"""Skill L1 索引预算与描述完整性（对齐 GA 第 3/4 轮）。"""
from __future__ import annotations

from types import SimpleNamespace


def _spec(name, desc, when=""):
    return SimpleNamespace(
        name=name, description=desc, when_to_use=when,
        disable_model_invocation=False,
    )


def test_default_budget_is_16k():
    from app.skill_store import build_skill_prompt
    import inspect

    sig = inspect.signature(build_skill_prompt)
    assert sig.parameters["char_budget"].default == 16384


def test_description_limit_is_1536():
    from app.skill_store import format_skill_listing

    long_desc = "触发器" * 600  # 1800 字符
    out = format_skill_listing([_spec("big", long_desc)], char_budget=100_000)
    line = out.splitlines()[0]
    # 截断点应在 1536 附近，而不是旧的 250
    assert 1000 < len(line) < 1700
    assert line.endswith("...")

def test_short_description_not_truncated():
    from app.skill_store import format_skill_listing

    out = format_skill_listing([_spec("small", "短描述")], char_budget=16_384)
    assert "短描述" in out and "..." not in out


def test_l1_keeps_trigger_sentence_when_budget_short():
    from app.skill_store import format_skill_listing

    specs = [
        _spec("a", "简短描述。"),
        _spec("b", "第二句触发条件。后面是无关的长描述。" + "冗长" * 300),
    ]
    out = format_skill_listing(specs, char_budget=80)
    # 预算不够时保留名称 + 第一句，而不是压成光名字
    assert "- b:" in out
    assert "第二句触发条件" in out

def test_disabled_skill_is_skipped():
    from app.skill_store import format_skill_listing

    spec = SimpleNamespace(name="x", description="d", when_to_use="",
                           disable_model_invocation=True)
    assert format_skill_listing([spec]) == ""
