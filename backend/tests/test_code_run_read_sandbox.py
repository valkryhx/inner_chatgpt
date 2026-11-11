"""code_run 的读边界：不能读其他用户的目录，也不能读保存用户数据的 DB。

背景（真实缺陷）：code_run 的审计钩子以前**只拦写不拦读**。于是一个普通登录用户
在对话框里让 agent「跑个脚本」，就能读出：

    ('u_f6ac05682e44', 'my-ds', ..., 'sk-pYE4Wg1u8')     ← 别人的 api_key
    ('u_f6ac05682e44', 'alice', '$2b$12$yZ5ft4u')         ← 密码哈希

而同一路径用 file_read 是被拒绝的 —— 两个工具判断不一致，弱的那条就是漏洞。

局限说明：进程内审计钩子不是安全边界（ctypes 可直接调 libc）。真正的多租户
隔离要靠容器，参见 docs/20261111-workspace沙箱逃逸修复方案.md §2.5 的 Aivory 对照。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.run_guard import (  # noqa: E402
    guard_argv,
    read_roots_for,
    shell_code_reason,
)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(REPO, "backend", "data", "app.db").replace("\\", "/")


def _run(code: str, user_id: str = "u_guard_test") -> str:
    async def _go():
        from app.builtin_tools import build_builtin_tools
        tools = {t.name: t for t in build_builtin_tools(user_id)}
        return await tools["code_run"].call({"code": code})

    return asyncio.run(_go())


def _blocked(out: str) -> bool:
    return "PermissionError" in out or "越界" in out or "不允许读取" in out


# ---------------- 必须拦住的读 ----------------
def test_cannot_read_platform_database():
    """核心回归：平台数据库（含所有用户 api_key / 密码哈希）必须读不到。"""
    out = _run(f'open("{DB_PATH}", "rb").read(16)')

    assert _blocked(out), f"数据库必须读不到，实际：{out[:300]}"


def test_cannot_connect_sqlite_to_platform_database():
    """sqlite3 走 C 层，不一定触发 open —— 必须单独拦它的审计事件。"""
    out = _run(f'''
import sqlite3
con = sqlite3.connect("{DB_PATH}")
print(con.execute("select api_key from user_llm_configs limit 1").fetchone())
''')

    assert _blocked(out), f"sqlite3 连接必须被拦，实际：{out[:300]}"
    assert "sk-" not in out, "绝不能泄漏 api_key"


def test_cannot_read_other_users_workspace():
    """核心需求：不能读其他用户的目录。"""
    other = os.path.join(REPO, "backend", "data", "workspaces").replace("\\", "/")
    out = _run(f'import os; print(os.listdir("{other}"))')

    assert _blocked(out), f"其他用户目录必须读不到，实际：{out[:300]}"


def test_cannot_read_repo_source():
    out = _run(f'open("{REPO}/backend/app/config.py").read()')

    assert _blocked(out), f"仓库源码必须读不到，实际：{out[:300]}"


def test_cannot_traverse_parent_directories():
    out = _run('import os; print(os.listdir("../../.."))')

    assert _blocked(out), f"目录穿越必须被拦，实际：{out[:300]}"


def test_cannot_read_server_logs():
    logs = os.path.join(REPO, "backend", "data", "logs").replace("\\", "/")
    out = _run(f'import os; print(os.listdir("{logs}"))')

    assert _blocked(out), f"服务端日志必须读不到，实际：{out[:300]}"


# ---------------- 正常用法不能被破坏 ----------------
def test_workspace_read_write_still_works():
    out = _run('''
open("ok.txt", "w").write("hello")
print("READBACK:", open("ok.txt").read())
''')

    assert not _blocked(out), f"workspace 内读写不该被拦，实际：{out[:300]}"
    assert "READBACK: hello" in out


def test_plain_computation_still_works():
    out = _run('print("SUM:", sum(range(10)))')

    assert not _blocked(out), out[:300]
    assert "SUM: 45" in out


def test_third_party_imports_still_work():
    """解释器自身目录必须可读，否则连 import 都过不去。"""
    out = _run('''
import json, sqlite3, pathlib
print("IMPORTS OK")
''')

    assert not _blocked(out), f"标准库必须可用，实际：{out[:300]}"
    assert "IMPORTS OK" in out


def test_can_still_read_own_workspace_files():
    async def _go():
        from app.builtin_tools import build_builtin_tools
        from app.workspace import user_workspace
        ws = user_workspace("u_guard_own")
        (ws / "mine.txt").write_text("私有数据", encoding="utf-8")
        tools = {t.name: t for t in build_builtin_tools("u_guard_own")}
        return await tools["code_run"].call({"code": 'print(open("mine.txt").read())'})

    out = asyncio.run(_go())
    assert "私有数据" in out, f"自己的文件必须能读，实际：{out[:300]}"


# ---------------- shell 分支 ----------------
@pytest.mark.parametrize("code", [
    'type "D:/git_codes/inner_chatgpt/backend/data/app.db"',
    "cat /etc/passwd",
    "cd .. && ls",
    'Get-Content "C:/Users/x/secret.txt"',
])
def test_shell_branch_rejects_absolute_and_traversal(code):
    reason = shell_code_reason(code)

    assert reason, f"shell 分支该拒绝：{code}"
    assert "数据库" in reason or "绝对路径" in reason or "跳出" in reason


def test_shell_branch_allows_relative_workspace_ops():
    assert shell_code_reason("echo hello > out.txt") is None
    assert shell_code_reason("python script.py") is None


# ---------------- 可读根定义与 file_read 一致 ----------------
def test_read_roots_match_file_read_definition():
    """两个工具对「能读什么」必须是同一份定义，否则弱的那条永远是漏洞。"""
    import app.run_guard as rg
    from app.workspace import safe_read_roots

    roots = read_roots_for("u_cmp")
    expected = [str(p) for p in safe_read_roots("u_cmp")]

    assert roots == expected, f"可读根必须与 workspace.safe_read_roots 一致\n{roots}\n{expected}"


def test_guard_argv_passes_write_root_and_read_roots():
    from pathlib import Path

    ws = Path("ws_root")
    argv = guard_argv("script.py", ws, user_id="u_argv")

    assert argv[0].endswith(("python.exe", "python3", "python"))
    assert argv[-3] == "script.py", "脚本路径"
    assert argv[-2] == str(ws), "写根"
    assert argv[-1] != "", "可读根不能为空"