"""MCP 并发额度：默认值必须能撑住「多子代理并行调 MCP」。

## 缺陷现场（2026-11-19 事故，见 docs/20261119-子代理卡死事故分析-MCP调用无超时.md §4）

用户让 2 个子代理并行搜索，两个子代理各占一个 MCP 槽位并卡死。
父代理试图自己搜索兜底时连续两次失败：

    [block 33 reasoning] "Capacity exceeded — probably because the two subagents
    are consuming MCP concurrency. So my hedge fails; I must rely on subagents."
    [block 34 text] "MCP 连接池被两个 agent 占满了（capacity_exceeded），
                     我暂时无法自己补搜，只能等两个子代理跑完。"

根因（`app/quota.py:38`）：

    user_mcp_calls: int = _env_int("USER_MAX_MCP_CALLS", 2)   # ← 只够两个并发

再加 `mcp_client.py` 里硬编码的 `timeout_s=2.0`（拿不到槽位就 2 秒放弃，
而不是排队），父代理被彻底饿死 —— **这掐掉了唯一的自救路径**。

## 修复后必须成立

1. 默认额度要能让「若干个子代理 + 父代理自己」同时持有 MCP 槽位；
2. 等待时间要可配置，不能硬编码 2 秒；
3. 槽位真的满了时，错误信息要说清是哪个闸门、当前水位多少（能自查）；
4. 正常路径（拿得到槽位）行为完全不变。
"""
from __future__ import annotations

import asyncio

import pytest

from app.quota import Limits, QuotaExceeded, QuotaManager


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 默认值

def test_default_user_mcp_calls_fits_parallel_subagents(monkeypatch):
    """默认用户级 MCP 额度必须 > 2。

    事故里就是因为它等于 2，两个子代理一占满，父代理就再也拿不到槽位。
    子代理上限默认是 8（`USER_MAX_ACTIVE_SUBAGENTS`），MCP 额度至少要
    能覆盖「几个子代理 + 父代理自己」，否则配置本身自相矛盾。
    """
    monkeypatch.delenv("USER_MAX_MCP_CALLS", raising=False)
    limits = Limits()
    assert limits.user_mcp_calls >= 8, (
        f"默认 user_mcp_calls={limits.user_mcp_calls} 太小："
        "两个子代理就能占满，父代理拿不到槽位（2026-11-19 事故）"
    )


def test_default_global_mcp_calls_not_less_than_user(monkeypatch):
    """全局闸门不能比用户闸门还小，否则用户级配额是摆设。"""
    monkeypatch.delenv("GLOBAL_MAX_MCP_CALLS", raising=False)
    monkeypatch.delenv("USER_MAX_MCP_CALLS", raising=False)
    limits = Limits()
    assert limits.global_mcp_calls >= limits.user_mcp_calls


def test_env_still_overrides_default():
    """环境变量必须仍然生效（运维要能按机器调）。

    ⚠️ 必须用**子进程**验证，不能在本进程 `monkeypatch.setenv` 后再 `Limits()`。
    因为 `Limits` 的字段默认值是 `_env_int(...)` 的**求值结果**，而 dataclass
    的默认值在**类定义时**（即模块导入时）就算好了。本进程改 `os.environ`
    不会影响已定义的类 —— 这是既有语义（环境变量 = 进程启动时读一次），
    不是本次要改的东西。用子进程才是它真实的使用场景。
    """
    import os
    import subprocess
    import sys

    env = {**os.environ, "USER_MAX_MCP_CALLS": "3"}
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'.');"
         "from app.quota import Limits; print(Limits().user_mcp_calls)"],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip().splitlines()[-1] == "3", out.stdout[-300:]


# ---------------------------------------------------------------- 排队语义

def test_wait_timeout_is_configurable(monkeypatch):
    """`mcp_call` 的等待时长要能配，不能硬编码 2s。"""
    import app.mcp_client as mc

    monkeypatch.setenv("MCP_CALL_QUEUE_WAIT_S", "7.5")
    assert mc.mcp_call_queue_wait_s() == pytest.approx(7.5, abs=1e-6)

    monkeypatch.delenv("MCP_CALL_QUEUE_WAIT_S", raising=False)
    v = mc.mcp_call_queue_wait_s()
    assert v > 2.0, f"默认排队等待 {v}s 太短，等于不排队（事故根因之一）"


def test_bad_queue_wait_falls_back(monkeypatch):
    """坏配置退回默认，不能让工具彻底不可用。"""
    import app.mcp_client as mc

    monkeypatch.setenv("MCP_CALL_QUEUE_WAIT_S", "abc")
    v = mc.mcp_call_queue_wait_s()
    assert v > 0


# ---------------------------------------------------------------- 水位可读

def test_exhausted_error_names_the_gate():
    """拿不到槽位时，错误必须说清是哪个闸门 + 当前水位。

    只说 `capacity_exceeded` 时，用户/运维分不清是自己多开标签页，
    还是槽位泄漏了 —— 这两种处置方式完全不同。
    """
    async def flow():
        q = QuotaManager(Limits(global_mcp_calls=8, user_mcp_calls=1))
        async with q.mcp_call("u1", timeout_s=0.05):
            with pytest.raises(QuotaExceeded) as ei:
                async with q.mcp_call("u1", timeout_s=0.05):
                    pass
            msg = str(ei.value)
            assert "MCP" in msg or "mcp" in msg, msg
            return msg

    msg = run(flow())
    assert "capacity_exceeded" in msg


# ---------------------------------------------------------------- 正常路径

def test_parallel_calls_within_limit_all_succeed():
    """额度够用时，并发调用不应互相排斥（回归：默认值调大后不能引入新问题）。"""
    async def flow():
        q = QuotaManager(Limits(global_mcp_calls=32, user_mcp_calls=8))
        held = []

        async def one(i):
            async with q.mcp_call("u1", timeout_s=1.0):
                held.append(i)
                await asyncio.sleep(0.05)

        await asyncio.gather(*(one(i) for i in range(6)))
        return len(held)

    assert run(flow()) == 6


def test_release_frees_slot_for_next_caller():
    """释放后下一个调用者必须能拿到槽位（不能泄漏）。"""
    async def flow():
        q = QuotaManager(Limits(global_mcp_calls=8, user_mcp_calls=1))
        async with q.mcp_call("u1", timeout_s=0.1):
            pass
        async with q.mcp_call("u1", timeout_s=0.1):
            return True

    assert run(flow()) is True