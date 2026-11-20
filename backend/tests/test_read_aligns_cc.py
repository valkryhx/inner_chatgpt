"""对齐 CC 的 Read 行为：默认 2000 行 + 截断横幅告知怎么翻页。

## 两条都来自 319 源码的可复算证据

| 事实 | 坐标 |
| --- | --- |
| 默认 2000 行 | `m0274_0x117329cf.js:604` `$tn = 2000` |
| 提示词同样写 2000 | `claude-code-fable-5.2.md:2331` |
| 截断横幅含翻页指引 | `m0591:148116` 的 `U7e` 模板 |

CC 的横幅原文（两档，按能否按行分页区分）：

    [Truncated: PARTIAL view — <file>: showing lines 1-N of M total
    (T tokens, cap G). Call <Read> with offset=N+1 limit=N for the next page,
    or <Grep> to find a specific section. Do NOT answer from this page alone
    if the answer may be further in the file.]

    [Truncated: PARTIAL view — <file>: showing the first X of Y characters
    (T tokens, cap G); this file has very long lines and cannot be paginated
    by line. Use <Grep> to find a specific section, or <Read> with
    offset/limit to page through it. Do NOT answer from this excerpt alone
    if the answer may be elsewhere in the file.]

## 为什么这两条重要（不是「抄得像」而已）

第一条：200 行让模型读 40KB 报告要多花 5 次调用。**是真实的轮次成本**。

第二条：只说「显示 3-50」模型得自己推「下一步用 start=51」。
CC 直接把它写出来。缺这句时模型经常重读同一段、或者放弃阅读直接瞎写。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

UID = "u_read_align"


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    from app import config as app_config
    from app import workspace as ws_mod

    root = tmp_path / UID
    root.mkdir()
    monkeypatch.setattr(ws_mod, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(app_config, "WORKSPACES_DIR", tmp_path)
    return root


def _tools():
    from app.builtin_tools import build_builtin_tools

    return {t.name: t for t in build_builtin_tools(user_id=UID)}


# ---------------- 默认 2000 行 ----------------

def test_default_read_lines_is_2000():
    """默认值必须对齐 CC 的 2000。"""
    from app.builtin_tools import DEFAULT_READ_LINES

    assert DEFAULT_READ_LINES == 2000, (
        f"DEFAULT_READ_LINES = {DEFAULT_READ_LINES}，应为 2000（对齐 CC 的 $tn=2000）")


def test_reads_1500_lines_in_one_call(ws):
    """1500 行文件必须**一次读完**（旧默认 200 时要 8 次）。"""
    text = "\n".join(f"line {i}" for i in range(1500)) + "\n"
    (ws / "big.txt").write_text(text, encoding="utf-8")
    out = asyncio.run(_tools()["file_read"].call({"path": "big.txt"}))
    assert "显示 1-1500" in out, f"没有一次读完 1500 行：{out[:160]}"
    assert "Truncated" not in out, "1500 行不该截断（默认已是 2000）"
    assert "line 1499" in out


def test_reads_2500_lines_in_two_calls(ws):
    """2500 行：第一次 1-2000（截断），第二次翻到末尾。"""
    text = "\n".join(f"line {i}" for i in range(2500)) + "\n"
    (ws / "huge.txt").write_text(text, encoding="utf-8")
    tools = _tools()
    out1 = asyncio.run(tools["file_read"].call({"path": "huge.txt"}))
    assert "显示 1-2000" in out1, f"第一次不是 1-2000：{out1[:160]}"
    out2 = asyncio.run(tools["file_read"].call({"path": "huge.txt", "start": 2001}))
    assert "显示 2001-2500" in out2, f"第二次不是 2001-2500：{out2[:160]}"
    assert "Truncated" not in out2, "读到末尾不该再截断"


def test_explicit_count_over_2000_is_clamped(ws):
    """显式 count 超过 2000 仍被夹住（与 CC 的硬上限一致）。"""
    text = "\n".join(f"line {i}" for i in range(3000)) + "\n"
    (ws / "x.txt").write_text(text, encoding="utf-8")
    out = asyncio.run(_tools()["file_read"].call({"path": "x.txt", "count": 99999}))
    assert "显示 1-2000" in out, f"显式超大 count 没被夹到 2000：{out[:160]}"


# ---------------- 截断横幅 ----------------

def test_truncated_banner_says_how_to_page(ws):
    """截断横幅必须**明确写出下一步怎么读**，不能只说「显示了几行」。"""
    text = "\n".join(f"line {i}" for i in range(2500)) + "\n"
    (ws / "huge.txt").write_text(text, encoding="utf-8")
    out = asyncio.run(_tools()["file_read"].call({"path": "huge.txt"}))
    assert "Truncated" in out
    # 必须给出具体参数，而不是让模型自己推
    assert "start=2001" in out, f"横幅没给出下一页的 start：{out[:400]}"
    assert "2500" in out, "横幅没给出总行数"


def test_truncated_banner_warns_against_answering_from_one_page(ws):
    """横幅必须提醒「不要只看这一页就下结论」—— CC 原文有此句。"""
    text = "\n".join(f"line {i}" for i in range(2500)) + "\n"
    (ws / "huge.txt").write_text(text, encoding="utf-8")
    out = asyncio.run(_tools()["file_read"].call({"path": "huge.txt"}))
    assert "不要只看" in out or "不要在只看" in out, (
        f"横幅缺少「不要只看这一页」的告警：{out[:400]}")


def test_no_banner_when_whole_file_fits(ws):
    """读完整文件时不该出现 Truncated 横幅（否则模型会一直以为没读完）。"""
    text = "\n".join(f"line {i}" for i in range(50)) + "\n"
    (ws / "small.txt").write_text(text, encoding="utf-8")
    out = asyncio.run(_tools()["file_read"].call({"path": "small.txt"}))
    assert "Truncated" not in out, f"小文件不该有截断横幅：{out[:200]}"


def test_banner_absent_when_reading_last_page(ws):
    """读到最后一页（end == total）不该有横幅。"""
    text = "\n".join(f"line {i}" for i in range(2500)) + "\n"
    (ws / "huge.txt").write_text(text, encoding="utf-8")
    out = asyncio.run(_tools()["file_read"].call(
        {"path": "huge.txt", "start": 2001}))
    assert "Truncated" not in out


def test_keyword_read_does_not_claim_partial(ws):
    """用 keyword 命中不算「读了开头」，不该谎称只读了片段。

    旧实现在 keyword 模式下 `complete=False`，但那是因为门禁。
    门禁已删，横幅只应反映**真的被截断**。
    """
    text = "\n".join(f"line {i}" for i in range(100)) + "\n"
    (ws / "k.txt").write_text(text, encoding="utf-8")
    out = asyncio.run(_tools()["file_read"].call(
        {"path": "k.txt", "keyword": "line 60"}))
    # 命中 60 起往后都读完了 => 到末尾，不该有 Truncated
    assert "Truncated" not in out, f"keyword 命中到末尾却标了截断：{out[:300]}"