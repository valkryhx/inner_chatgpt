"""file_glob / file_grep：搜索工具（TDD 红先）。"""
from __future__ import annotations

import json

from app import workspace

UID = "u_test"


def _tree(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "workspaces")
    root = tmp_path / "workspaces" / UID
    (root / "src").mkdir(parents=True)
    (root / "vendor" / "__pycache__").mkdir(parents=True)
    (root / "src" / "a.py").write_text("# TODO: alpha\nprint('a')\n", encoding="utf-8")
    (root / "src" / "b.py").write_text("# TODO: beta\nprint('b')\n", encoding="utf-8")
    (root / "src" / "c.txt").write_text("no todo here\n", encoding="utf-8")
    (root / "vendor" / "__pycache__" / "junk.py").write_text("# TODO: junk\n", encoding="utf-8")
    return root


def test_glob_basic_pattern(tmp_path, monkeypatch):
    from app.file_search import file_glob

    _tree(tmp_path, monkeypatch)
    out = json.loads(file_glob(UID, "**/*.py"))
    assert out["ok"] is True
    paths = out["matches"]
    assert "src/a.py" in paths and "src/b.py" in paths
    assert all("vendor/__pycache__" not in p for p in paths)


def test_glob_returns_relative_paths(tmp_path, monkeypatch):
    from app.file_search import file_glob

    root = _tree(tmp_path, monkeypatch)
    out = json.loads(file_glob(UID, "**/*.py"))
    assert all(not p.startswith(str(root)) for p in out["matches"])


def test_glob_truncates_at_100_and_says_so(tmp_path, monkeypatch):
    from app.file_search import file_glob

    root = _tree(tmp_path, monkeypatch)
    for i in range(120):
        (root / "many").mkdir(exist_ok=True)
        (root / "many" / f"f{i:03d}.log").write_text("x", encoding="utf-8")
    out = json.loads(file_glob(UID, "**/*.log"))
    assert len(out["matches"]) == 100
    assert "120" in out["note"]


def test_glob_excludes_vendor_dirs(tmp_path, monkeypatch):
    from app.file_search import file_glob

    _tree(tmp_path, monkeypatch)
    out = json.loads(file_glob(UID, "**/*.py"))
    assert not any("__pycache__" in p for p in out["matches"])


def test_glob_no_match_is_ok_not_error(tmp_path, monkeypatch):
    from app.file_search import file_glob

    _tree(tmp_path, monkeypatch)
    out = json.loads(file_glob(UID, "**/*.nope"))
    assert out["ok"] is True and out["matches"] == []


def test_glob_respects_workspace_boundary(tmp_path, monkeypatch):
    from app.file_search import file_glob

    _tree(tmp_path, monkeypatch)
    out = json.loads(file_glob(UID, "**/*.py", path="../.."))
    assert out["ok"] is False


def test_grep_content_mode(tmp_path, monkeypatch):
    from app.file_search import file_grep

    _tree(tmp_path, monkeypatch)
    out = json.loads(file_grep(UID, "TODO"))
    assert out["ok"] is True
    text = "\n".join(out["lines"])
    assert "src/a.py:1:" in text and "alpha" in text
    assert "src/b.py:1:" in text and "beta" in text


def test_grep_files_with_matches_mode(tmp_path, monkeypatch):
    from app.file_search import file_grep

    _tree(tmp_path, monkeypatch)
    out = json.loads(file_grep(UID, "TODO", output_mode="files_with_matches"))
    assert sorted(out["files"]) == ["src/a.py", "src/b.py"]


def test_grep_count_mode(tmp_path, monkeypatch):
    from app.file_search import file_grep

    _tree(tmp_path, monkeypatch)
    out = json.loads(file_grep(UID, "TODO", output_mode="count"))
    assert out["counts"] == {"src/a.py": 1, "src/b.py": 1}


def test_grep_ignore_case(tmp_path, monkeypatch):
    from app.file_search import file_grep

    root = _tree(tmp_path, monkeypatch)
    (root / "src" / "d.py").write_text("# todo: lower\n", encoding="utf-8")
    assert json.loads(file_grep(UID, "TODO", ignore_case=True))["total"] == 4


def test_grep_context_lines(tmp_path, monkeypatch):
    from app.file_search import file_grep

    _tree(tmp_path, monkeypatch)
    out = json.loads(file_grep(UID, "alpha", context=1))
    joined = "\n".join(out["lines"])
    assert "print('a')" in joined


def test_grep_glob_filter(tmp_path, monkeypatch):
    from app.file_search import file_grep

    _tree(tmp_path, monkeypatch)
    out = json.loads(file_grep(UID, "TODO", glob="*.txt"))
    assert out["total"] == 0


def test_grep_head_limit(tmp_path, monkeypatch):
    from app.file_search import file_grep

    root = _tree(tmp_path, monkeypatch)
    (root / "src" / "many.log").write_text("hit\n" * 60, encoding="utf-8")
    out = json.loads(file_grep(UID, "hit", head_limit=10))
    assert len(out["lines"]) <= 10
    assert out["truncated"] is True


def test_grep_invalid_regex_is_clear_error(tmp_path, monkeypatch):
    from app.file_search import file_grep

    _tree(tmp_path, monkeypatch)
    out = json.loads(file_grep(UID, "([unclosed"))
    assert out["ok"] is False
    assert "regex" in out["error"].lower() or "正则" in out["error"]
    assert "Traceback" not in out["error"]


def test_grep_falls_back_to_python_without_rg(tmp_path, monkeypatch):
    from app import file_search

    _tree(tmp_path, monkeypatch)
    monkeypatch.setattr(file_search.shutil, "which", lambda name: None)
    out = json.loads(file_search.file_grep(UID, "TODO"))
    assert out["ok"] is True and out["total"] == 2


def test_tools_are_registered():
    from app.builtin_tools import build_builtin_tools

    names = [t.name for t in build_builtin_tools(user_id=UID)]
    assert "file_glob" in names and "file_grep" in names


def test_file_glob_description_mentions_total_count():
    from app.builtin_tools import build_builtin_tools

    tool = {t.name: t for t in build_builtin_tools(user_id=UID)}["file_glob"]
    assert "total" in tool.description
    assert "统计" in tool.description
