"""`code_run` 的 shell 类型选择：不该让模型在「powershell」上撞墙。

## 背景（真实故障，2026-11-18 用户自测发现）

`code_run` 的 schema 里 `type` 只有两个枚举值：

    {"enum": ["python", "powershell"], "description": "默认 python"}

这是**宿主 Windows 视角**。模型想做 shell 操作（`ls` / `mkdir` / `pip install`）
只能选 `powershell`；一旦这个用户被沙箱接管，容器是 Debian，没有 powershell，
必然失败。模型要花 1-2 轮才发现「应该改用 python + subprocess」——
纯浪费，而且这段时间它会认为环境坏了。

## 这里锁的契约

1. schema 里必须有 `sh`/`bash` 选项，且描述说明「沙箱内用 sh」；
2. 沙箱路径下 `type=powershell` 返回**可操作的**错误（告诉模型改用哪个），
   不是一句「不可用」；
3. 进程内路径下 `type` 的语义不能变（Windows 上仍是 powershell）。
"""
from __future__ import annotations

import asyncio

import pytest

from app import builtin_tools


def run(coro):
    return asyncio.run(coro)


def schema_of(name: str, uid: str = "u_x") -> dict:
    """取出某个内置工具的 JSON schema。"""
    items = builtin_tools.build_builtin_tools(uid, include_memory=False)
    for t in items:
        if t.name == name:
            return t.input_schema
    raise AssertionError(f"没有工具 {name}")


# ---------------- schema 必须给出跨平台可用的选项 ----------------


def test_code_run_schema_offers_sh():
    """沙箱是 Debian，必须能选 sh —— 只给 python/powershell 是设计缺陷。"""
    s = schema_of("code_run")
    types = s["properties"]["type"]["enum"]
    assert "sh" in types or "bash" in types, \
        f"type 枚举里没有 sh/bash，沙箱里无法跑 shell：{types}"


def test_code_run_schema_type_description_mentions_sandbox():
    """描述里必须点明平台差异，否则模型只能靠试错发现。"""
    s = schema_of("code_run")
    desc = s["properties"]["type"].get("description", "") + \
        s.get("description", "")
    low = desc.lower()
    assert any(k in low or k in desc for k in ("sh", "bash", "沙箱", "linux", "容器")), \
        f"type 描述没有说明 shell 平台差异：{desc!r}"


def test_code_run_accepts_sh_alias():
    """`sh` / `bash` / `shell` 都必须被当作 shell 处理（不能落到 else 报错）。"""
    from app.sandbox.manager import _SH_TYPES
    for alias in ("sh", "bash", "shell"):
        assert alias in _SH_TYPES, f"{alias} 不是 shell 类型"


def test_powershell_error_is_actionable():
    """沙箱下选 powershell，错误信息必须告诉模型改用哪个类型。

    只说「不可用」= 模型还得再猜一次；给出替代 = 下一轮就能改对。
    """
    from app.sandbox.manager import SandboxManager
    from app.sandbox.config import SandboxConfig

    cfg = SandboxConfig(mode="strict", mvp_users=("u_x",))
    mgr = SandboxManager(cfg=cfg, pool=object())

    handled, out, _ = run(mgr.exec_code("u_x", "dir", code_type="powershell"))
    assert handled is True
    assert "python" in out.lower() or "sh" in out.lower(), \
        f"错误信息没有给出替代方案：{out!r}"
    # 不能只说「不可用」就完事
    assert len(out) > 20, out