"""预算层 / 退出码与内置工具的集成（TDD 红先）。"""
from __future__ import annotations

import asyncio
import json

from app import workspace

UID = "u_test"


def _ws(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "workspaces")
    root = tmp_path / "workspaces" / UID
    root.mkdir(parents=True)
    return root


def _tool(name: str):
    from app.builtin_tools import build_builtin_tools

    return {t.name: t for t in build_builtin_tools(user_id=UID)}[name]


def test_file_read_large_file_gets_persisted(tmp_path, monkeypatch):
    root = _ws(tmp_path, monkeypatch)
    body = "\n".join(f"{i:05d}" + "x" * 60 for i in range(2000))
    (root / "big.txt").write_text(body, encoding="utf-8")
    out = asyncio.run(_tool("file_read").call({"path": "big.txt", "count": 2000}))
    assert ".hlaudz/tool_results/" in out.replace("\\", "/")
    assert "omitted" in out


def test_code_run_large_output_gets_persisted(tmp_path, monkeypatch):
    _ws(tmp_path, monkeypatch)
    code = "print('y' * 60000)"
    out = asyncio.run(_tool("code_run").call({"code": code, "type": "python"}))
    assert "omitted" in out
    assert "tool_results" in out.replace("\\", "/")


def test_code_run_header_marks_success(tmp_path, monkeypatch):
    _ws(tmp_path, monkeypatch)
    out = asyncio.run(_tool("code_run").call({"code": "print(1)", "type": "python"}))
    assert "exit=0" in out
    assert "informational" not in out


def test_format_code_run_header_informational():
    from app.builtin_tools import format_code_run_header

    header = format_code_run_header("bash", "grep -rn x .", 1, "/w")
    assert "exit=1" in header
    assert "no matches" in header


def test_format_code_run_header_python_is_plain():
    from app.builtin_tools import format_code_run_header

    header = format_code_run_header("python", "raise SystemExit(1)", 1, "/w")
    assert "exit=1" in header
    assert "no matches" not in header


def test_file_glob_output_is_json_and_budgeted(tmp_path, monkeypatch):
    root = _ws(tmp_path, monkeypatch)
    for i in range(30):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")
    out = asyncio.run(_tool("file_glob").call({"pattern": "*.txt"}))
    data = json.loads(out)
    assert data["ok"] is True and len(data["matches"]) == 30
