"""工具结果预算层：per-tool 上限 + 超限落盘 + 头尾预览（TDD 红先）。"""
from __future__ import annotations

import pytest


def _big(n: int) -> str:
    # 每行 20 字符，便于核对省略量
    return "".join(f"{i:04d}abcdefghijklmnop\n" for i in range(n // 21 + 1))[:n]


def test_small_output_passthrough(tmp_path):
    from app.tool_budget import apply_budget

    text = "hello world"
    out = apply_budget("file_read", text, persist_dir=tmp_path)
    assert out == text
    assert list(tmp_path.iterdir()) == []


def test_per_tool_limit_applies(tmp_path):
    from app.tool_budget import apply_budget

    # grep 上限 20k，25k 必须被处理；glob 上限 100k，80k 放行
    grep_out = apply_budget("file_grep", _big(25_000), persist_dir=tmp_path)
    assert len(grep_out) < 25_000
    glob_out = apply_budget("file_glob", _big(80_000), persist_dir=tmp_path)
    assert glob_out == _big(80_000)


def test_over_limit_persists_and_previews(tmp_path):
    from app.tool_budget import apply_budget

    text = _big(60_000)
    out = apply_budget("file_read", text, persist_dir=tmp_path)
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    saved = files[0].read_text(encoding="utf-8")
    assert saved == text
    assert files[0].as_posix() in out.replace("\\", "/")
    assert out.startswith(text[:2000])
    assert out.endswith(text[-2000:])


def test_persisted_path_is_readable(tmp_path):
    from app.tool_budget import apply_budget

    out = apply_budget("code_run", _big(40_000), persist_dir=tmp_path)
    path = next(p for p in tmp_path.iterdir())
    assert path.is_file()
    assert path.resolve().as_posix() in out.replace("\\", "/")


def test_omitted_char_count_is_exact(tmp_path):
    from app.tool_budget import apply_budget

    text = _big(30_000)
    out = apply_budget("file_grep", text, persist_dir=tmp_path)
    omitted = len(text) - 2000 - 2000
    assert f"{omitted:,}" in out


def test_persist_limit_caps_stored_bytes(tmp_path):
    from app.tool_budget import apply_budget

    text = _big(600_000)
    out = apply_budget("file_read", text, persist_dir=tmp_path)
    saved = next(p for p in tmp_path.iterdir()).read_text(encoding="utf-8")
    assert len(saved) == 500_000
    assert "已截断" in out or "truncated" in out.lower()


def test_persist_failure_degrades_gracefully(tmp_path):
    from app.tool_budget import apply_budget

    # 跨平台模拟落盘失败：把 persist_dir 指向一个普通文件（mkdir 必然失败）
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("x", encoding="utf-8")
    out = apply_budget("file_read", _big(60_000), persist_dir=blocker)
    assert len(out) <= 10_000
    assert "not persisted" in out or "未落盘" in out


def test_env_override_max_chars(tmp_path, monkeypatch):
    from app.tool_budget import apply_budget

    monkeypatch.setenv("HLZ_TOOL_RESULT_MAX_CHARS", "1000")
    out = apply_budget("file_read", _big(5_000), persist_dir=tmp_path)
    assert len(out) < 5_000


def test_unknown_tool_uses_default_limit(tmp_path):
    from app.tool_budget import apply_budget

    out = apply_budget("some_new_tool", _big(60_000), persist_dir=tmp_path)
    assert len(out) < 60_000


def test_budget_does_not_write_outside_persist_dir(tmp_path):
    from app.tool_budget import apply_budget

    out = apply_budget("file_read", _big(60_000), persist_dir=tmp_path)
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(written) == 1
    assert tmp_path.resolve().as_posix() in written[0].resolve().as_posix()
