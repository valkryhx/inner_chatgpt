"""写前必读门禁（`ReadRegistry` 单元测试）。

## ⚠️ 这个门禁已在 **工具层被摘除**（2026-11-20）

本文件测的是 `ReadRegistry` 这个**类**的行为，它本身仍然可用、仍然正确。
但 `file_write` / `file_patch` **不再调用 `check()`** ——
见 `docs/20261120-删除写前必读门禁.md` 与 `tests/test_no_read_before_write.py`。

摘除的理由（外部 + 真机双证据）：

1. `claude-code-fable-5.2.md:133` 明确推翻了 Edit 的读前置：
   "Edit works there without a prior Read"
2. `DEFAULT_READ_LINES = 200` 让大文件的 `complete` 判定**恒为假**，
   覆盖写被必然拒绝 —— 2026-11-20 跑 Agent Team 时 writer_c 实际卡在这

保留文件而不删的理由：这个类仍有记录/比对能力，将来若要重做「冲突探测」
（不是硬门禁，而是提示「你上次读之后文件被改过」）可以直接复用。

两条工具级测试（`test_tool_write_without_read_is_rejected` /
`test_tool_patch_requires_read`）已删除 —— 它们断言的正是被移除的行为，
留着只会变成假的期望。
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from app import workspace

UID = "u_test"


def _ws(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "workspaces")
    root = tmp_path / "workspaces" / UID
    root.mkdir(parents=True)
    return root


def _reg():
    from app.read_registry import ReadRegistry

    return ReadRegistry()


def test_unread_write_is_rejected():
    reg = _reg()
    ok, reason = reg.check(__file__)
    assert ok is False and "file_read" in reason


def test_partial_read_is_rejected(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\n", encoding="utf-8")
    reg = _reg()
    reg.record(p, complete=False)
    ok, reason = reg.check(p)
    assert ok is False and ("完整" in reason or "complete" in reason)


def test_full_read_allows_write(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\n", encoding="utf-8")
    reg = _reg()
    reg.record(p, complete=True)
    assert reg.check(p)[0] is True


def test_external_modification_is_rejected(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\n", encoding="utf-8")
    reg = _reg()
    reg.record(p, complete=True)
    time.sleep(0.01)
    p.write_text("changed\n", encoding="utf-8")
    ok, reason = reg.check(p)
    assert ok is False and ("修改" in reason or "changed" in reason)


def test_touch_without_content_change_is_allowed(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("same\n", encoding="utf-8")
    reg = _reg()
    reg.record(p, complete=True)
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    assert reg.check(p)[0] is True


def test_new_file_is_allowed(tmp_path):
    reg = _reg()
    assert reg.check(tmp_path / "brand_new.txt")[0] is True


def test_records_are_isolated_between_instances(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x", encoding="utf-8")
    _reg().record(p, complete=True)
    assert _reg().check(p)[0] is False


def test_entry_cap_evicts_oldest(tmp_path):
    reg = _reg()
    reg.max_entries = 3
    for i in range(5):
        p = tmp_path / f"f{i}.txt"
        p.write_text("x", encoding="utf-8")
        reg.record(p, complete=True)
    assert reg.check(tmp_path / "f4.txt")[0] is True
    assert reg.check(tmp_path / "f0.txt")[0] is False


def _tool(name: str, **kw):
    from app.builtin_tools import build_builtin_tools

    return {t.name: t for t in build_builtin_tools(user_id=UID, **kw)}[name]


def test_tool_write_after_read_is_allowed(tmp_path, monkeypatch):
    root = _ws(tmp_path, monkeypatch)
    (root / "a.txt").write_text("old\n", encoding="utf-8")
    # registry 绑定在一次 build 上：同一组工具共享读取记录（生产里也是这样）
    from app.builtin_tools import build_builtin_tools

    tools = {t.name: t for t in build_builtin_tools(user_id=UID)}
    asyncio.run(tools["file_read"].call({"path": "a.txt"}))
    out = asyncio.run(tools["file_write"].call({"path": "a.txt", "content": "new\n"}))
    assert "已写入" in out
    assert (root / "a.txt").read_text(encoding="utf-8") == "new\n"


def test_tool_append_does_not_require_read(tmp_path, monkeypatch):
    root = _ws(tmp_path, monkeypatch)
    (root / "a.txt").write_text("old\n", encoding="utf-8")
    out = asyncio.run(_tool("file_write").call(
        {"path": "a.txt", "content": "more\n", "mode": "append"}))
    assert "已写入" in out


def test_tool_new_file_does_not_require_read(tmp_path, monkeypatch):
    _ws(tmp_path, monkeypatch)
    out = asyncio.run(_tool("file_write").call({"path": "new.txt", "content": "x"}))
    assert "已写入" in out


def test_tool_patch_allows_edit_without_prior_read(tmp_path, monkeypatch):
    """★ 与提示词对齐：Edit 在 workspace 内不需要先读。

    原测试断言的是「patch 必须 file_read」（`assert "file_read" in out`），
    那条已被 `claude-code-fable-5.2.md:133` 推翻，改为断言反面。
    """
    root = _ws(tmp_path, monkeypatch)
    (root / "a.txt").write_text("old\n", encoding="utf-8")
    out = asyncio.run(_tool("file_patch").call(
        {"path": "a.txt", "old_content": "old", "new_content": "new"}))
    assert "已局部修改" in out, f"仍被门禁拦住：{out}"
    assert (root / "a.txt").read_text(encoding="utf-8") == "new\n"
