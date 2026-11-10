"""退出码语义化：把「非零 = 错误」拆成 error vs informational。

grep 无匹配返回 1、diff 有差异返回 1、test 判断为假返回 1——这些都不是错误，
但模型看到 exit=1 会重试或改方案。这里补一句人类可读的说明。
"""
from __future__ import annotations

SHELL_TYPES = frozenset({"bash", "sh", "shell", "powershell", "ps1", "pwsh"})

_NOTE_BY_COMMAND = {
    "grep": "no matches",
    "egrep": "no matches",
    "fgrep": "no matches",
    "rg": "no matches",
    "diff": "files differ",
    "cmp": "files differ",
    "test": "condition is false",
    "[": "condition is false",
    "find": "some paths unreadable",
}

_SKIP_PREFIX = ("sudo", "command", "env", "time", "nohup")


def _first_word(command: str) -> str:
    for raw in (command or "").strip().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for token in line.split():
            if "=" in token and not token.startswith("-"):
                continue  # 跳过 VAR=value
            if token in _SKIP_PREFIX:
                continue
            return token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return ""


def _is_git_exit_code(command: str) -> bool:
    toks = (command or "").split()
    return bool(toks) and toks[0].endswith("git") and "diff" in toks and "--exit-code" in toks


def exit_code_note(command: str, exit_code: int) -> str:
    """人类可读的补充说明；没有可说的返回空串。"""
    if exit_code == 0:
        return ""
    word = _first_word(command)
    if _is_git_exit_code(command):
        return "differences found"
    note = _NOTE_BY_COMMAND.get(word)
    if note:
        return f"{word}: {note}"
    if word == "git" and "diff" in (command or ""):
        return "git: differences found"
    return ""


def classify_exit_code(
    tool_name: str,
    code_type: str,
    command: str,
    exit_code: int,
) -> str:
    """success | error | informational。只对 shell 类型做语义化。"""
    if exit_code == 0:
        return "success"
    if (code_type or "").lower() not in SHELL_TYPES:
        return "error"
    return "informational" if exit_code_note(command, exit_code) else "error"
