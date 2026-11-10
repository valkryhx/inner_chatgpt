"""退出码语义化：grep/diff/test/find 的非零是 informational，不是 error。"""
from __future__ import annotations


def _cls(command: str, exit_code: int, *, code_type: str = "bash"):
    from app.exit_codes import classify_exit_code

    return classify_exit_code("code_run", code_type, command, exit_code)


def test_grep_no_match_is_informational():
    assert _cls("grep -rn foo .", 1) == "informational"


def test_rg_no_match_is_informational():
    assert _cls("rg foo backend/", 1) == "informational"


def test_diff_has_differences_is_informational():
    assert _cls("diff a.txt b.txt", 1) == "informational"


def test_test_command_false_is_informational():
    assert _cls("test -f nope.txt", 1) == "informational"


def test_find_permission_is_informational():
    assert _cls("find . -name x", 1) == "informational"


def test_git_diff_exit_code_is_informational():
    assert _cls("git diff --exit-code", 1) == "informational"


def test_unknown_command_exit_1_is_error():
    assert _cls("python script.py", 1) == "error"


def test_python_type_keeps_binary_semantics():
    # 脚本的返回值没有公认约定，即使命令文本里出现 grep 也不改判
    assert _cls("grep x", 1, code_type="python") == "error"


def test_header_includes_human_note():
    from app.exit_codes import exit_code_note

    assert "no matches" in exit_code_note("grep -rn foo .", 1)
    assert exit_code_note("python x.py", 0) == ""


def test_success_is_success():
    assert _cls("grep foo .", 0) == "success"
