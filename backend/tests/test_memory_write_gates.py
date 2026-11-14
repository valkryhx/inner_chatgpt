"""M4 前置：提取闸门的判定逻辑（纯函数，不涉编排）。

对应 ``docs/20261112-Hlaudz记忆系统实施方案.md`` §4.2 的四道闸与 §7.1 的 M-6 / M-7。

这一批用例的价值在于：**闸 2 的 CJK 计数错法会让中文产品「记忆永远不写」**，
而拿英文测全绿。所以 M-6 是防回归的关键用例，不是补充。
"""
from __future__ import annotations

import pytest

from app import memory_write as mw


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """默认打开自动提取，让闸门测试聚焦在闸本身而不是总开关。"""
    monkeypatch.setenv("HLZ_MEMORY_AUTO_WRITE", "1")
    monkeypatch.delenv("HLZ_MEMORY_DISABLED", raising=False)


def _msgs(*texts, role="user"):
    return [{"role": role, "content": t} for t in texts]


# ---------------- M-6 CJK prose 计数（防回归关键） ----------------
def test_m6_cjk_200_chars_counts_as_100_words():
    """200 个汉字 = 100 词（CJK 2 字算 1 词），必须 ≥ 默认阈值。

    按 ``split()`` 实现会数成 1 词 —— 中文用户的记忆永远不写。
    """
    text = "这是一段完全由中文组成的对话内容不包含任何英文字符我们需要确认计数器能正确处理它" * 3
    assert mw.prose_word_count(text) >= 50
    assert mw.has_enough_prose(_msgs(text), min_words=50) is True
    # 反证：朴素 split() 只有 1 个元素
    assert len(text.split()) == 1


def test_m6_mixed_cjk_latin():
    text = "用 pytest 跑测试，然后用 git commit 提交"
    # latin: 用/pytest/跑测试，然后用/git/commit/提交 -> 6 段
    # cjk: 用(1) 跑测试(3) ，然后用(4) 提交(2) = 10 -> 5 词
    assert mw.prose_word_count(text) == len(text.split()) + len(
        [c for c in text if "一" <= c <= "鿿"]) // 2


def test_m6_empty_and_pure_tool_results():
    assert mw.prose_word_count("") == 0
    tool_only = [{"role": "tool", "content": "这是一段很长的工具输出" * 50}]
    assert mw.has_enough_prose(tool_only, min_words=10) is False, \
        "tool 结果不算 prose —— 跑一次测试两千行，但没有值得记的事"


def test_m6_volatile_blocks_not_counted():
    """注入块（记忆本身）不能算作「用户说了话」，否则会自我强化。"""
    msgs = [{"role": "user", "content": "这是一段很长的记忆块内容" * 30, "_volatile": True}]
    assert mw.has_enough_prose(msgs, min_words=10) is False


def test_m6_assistant_text_counts():
    msgs = [{"role": "assistant", "content": "好的，以后都用中文回答你"}]
    assert mw.has_enough_prose(msgs, min_words=3) is True


# ---------------- M-7 闸 3 频率 ----------------
def test_m7_frequency_gate_every_3_turns():
    assert mw.turn_gate_open(1, every_n=3) is False
    assert mw.turn_gate_open(2, every_n=3) is False
    assert mw.turn_gate_open(3, every_n=3) is True
    assert mw.turn_gate_open(4, every_n=3) is False
    assert mw.turn_gate_open(6, every_n=3) is True


def test_m7_frequency_gate_every_1_turns():
    for n in (1, 2, 3, 100):
        assert mw.turn_gate_open(n, every_n=1) is True


def test_m7_frequency_gate_zero_turn_never_opens():
    assert mw.turn_gate_open(0, every_n=1) is False


# ---------------- 闸 1 直写 ----------------
def test_gate1_direct_write_blocks_extraction():
    msgs = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "memory_write", "arguments": "{}"}}],
    }]
    assert mw.already_wrote_in_range(msgs) is True
    d = mw.evaluate_gates(_msgs("一段足够长的中文对话用来通过闸二"), turn_count=3,
                          min_words=3)
    assert d.allowed is True  # 这份没直写，应该放行

    d2 = mw.evaluate_gates(msgs, turn_count=3, min_words=3)
    assert d2.allowed is False
    assert d2.gate == "direct-write"


def test_gate1_ignores_other_tools():
    msgs = [{
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "file_read", "arguments": "{}"}}],
    }]
    assert mw.already_wrote_in_range(msgs) is False


# ---------------- 闸 4 权限 ----------------
def test_gate4_permission_blocks():
    d = mw.evaluate_gates(_msgs("足够长的中文内容" * 5), turn_count=3,
                          min_words=3, permission_ok=False)
    assert d.allowed is False and d.gate == "permission"


# ---------------- 总开关 ----------------
def test_disabled_when_auto_write_off(monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_AUTO_WRITE", "0")
    d = mw.evaluate_gates(_msgs("足够长的中文内容" * 5), turn_count=3, min_words=3)
    assert d.allowed is False and d.gate == "disabled"
    assert "HLZ_MEMORY_AUTO_WRITE" in d.reason


def test_hard_disabled_beats_auto_write(monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_DISABLED", "1")
    d = mw.evaluate_gates(_msgs("足够长的中文内容" * 5), turn_count=3, min_words=3)
    assert d.allowed is False and d.gate == "disabled"
    assert "HLZ_MEMORY_DISABLED" in d.reason


def test_auto_write_defaults_off(monkeypatch):
    """默认关闭是产品决定，不是遗漏 —— 有测试锁住它。"""
    monkeypatch.delenv("HLZ_MEMORY_AUTO_WRITE", raising=False)
    from app import memory_store as ms
    assert ms.auto_write_enabled() is False
    d = mw.evaluate_gates(_msgs("足够长" * 10), turn_count=3, min_words=3)
    assert d.allowed is False


# ---------------- 全通过 ----------------
def test_all_gates_pass():
    d = mw.evaluate_gates(_msgs("用户说他希望以后都被叫做老徐，并且始终用中文回答。" * 2),
                          turn_count=3, min_words=3)
    assert d.allowed is True and d is not None
    assert bool(d) is True


# ---------------- 提示词 ----------------
def test_extract_prompt_mentions_all_four_kinds():
    for kind in ("user", "feedback", "project", "reference"):
        assert f"`{kind}`" in mw.EXTRACT_PROMPT


def test_extract_prompt_forbids_secrets():
    assert "密码" in mw.EXTRACT_PROMPT
    assert "不要写" in mw.EXTRACT_PROMPT or "一律不写" in mw.EXTRACT_PROMPT


def test_build_extract_prompt_truncates_keeping_head_and_tail():
    head = "开头：用户说以后都用中文。"
    body = "中" * 20000
    tail = "结尾：用户说他叫老徐。"
    out = mw.build_extract_prompt(head + body + tail, max_chars=1200)
    assert head in out, "开头（最常交代偏好的地方）必须保留"
    assert tail in out
    assert "省略" in out
    assert len(out) < 20000


def test_conversation_from_skips_volatile_and_tools():
    msgs = [
        {"role": "user", "content": "叫我老徐"},
        {"role": "user", "content": "[记忆] x", "_volatile": True},
        {"role": "assistant", "content": "好的"},
        {"role": "tool", "content": "工具结果不该进提取对话"},
    ]
    text = mw.conversation_from(msgs)
    assert "叫我老徐" in text and "好的" in text
    assert "[记忆]" not in text
    assert "工具结果" not in text


def test_conversation_from_limits_items():
    msgs = [{"role": "user", "content": f"第{i}条"} for i in range(100)]
    text = mw.conversation_from(msgs, limit=10)
    assert "第99条" in text
    assert "第0条" not in text
