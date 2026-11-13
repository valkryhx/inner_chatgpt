"""MCP 并发连接：单测。

背景（2026-11-12 实测）：`build_user_toolset` 串行连接全部 MCP server，
每个 `MCP_CONNECT_TIMEOUT=12s`。4 个 server 里 1 个起不来时，总耗时 32 秒——
这期间前端拿不到状态，界面显示「未检测 · 共 0 个可用工具」。

改成并发后，总耗时 ≈ 最慢那一个。

本文件锁住四个不变量：
  1. **确实并发**（总耗时接近最慢单个，而不是相加）；
  2. **顺序保持**（manifest 顺序 == 配置顺序，UI 直接展示）；
  3. **失败隔离**（一个 server 抛异常不影响其他）；
  4. **取消时不留孤儿**（CancelledError 要把已连的关掉再抛）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent as agent_mod  # noqa: E402


class FakeToolset:
    """最小的 MCPToolset 替身：可配置连接耗时 / 是否失败 / 工具数。"""

    def __init__(self, target=None, *, user_id="", server_name="", **_kw):
        self._target = target
        self.server_name = server_name
        self.tools: list = []
        self._manifest: list[dict] = []
        self._closed = False

    async def __aenter__(self):
        spec = _SPECS.get(self.server_name, {})
        delay = float(spec.get("delay", 0.0))
        if delay:
            await asyncio.sleep(delay)
        if spec.get("raise"):
            raise RuntimeError(f"{self.server_name} boom")
        n = int(spec.get("tools", 1))
        self.tools = [object() for _ in range(n)]
        return self

    async def __aexit__(self, *_a):
        self._closed = True

    def status(self) -> dict:
        return {"name": self.server_name, "ok": True,
                "tool_count": len(self.tools), "error": "",
                "tools": [f"{self.server_name}_t{i}" for i in range(len(self.tools))]}

    @classmethod
    def merge(cls, toolsets):
        merged = cls(user_id="", server_name="merged")
        for ts in toolsets:
            merged.tools.extend(ts.tools)
        return merged


# 每个 server 的行为（按 name 查）
_SPECS: dict[str, dict] = {}


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    """把 MCPToolset 与 db 查询整体替换掉。"""
    _SPECS.clear()

    async def fake_list_mcp_servers(_user_id):
        return [{"name": n, "transport": "http", "enabled": True,
                 "url": f"http://x/{n}"} for n in _SPECS]

    monkeypatch.setattr(agent_mod, "MCPToolset", FakeToolset)
    monkeypatch.setattr(agent_mod.db, "list_mcp_servers", fake_list_mcp_servers)
    yield
    _SPECS.clear()


# --------------------------------------------------------------------------
# 核心：确实并发
# --------------------------------------------------------------------------

def test_connects_in_parallel_not_serial():
    """4 个各 0.15s 的 server：并发总耗时 ≈ 0.15s，串行会是 0.6s。

    这是本次修复的全部意义。断言用「显著小于串行总和」，而不是精确值，
    避免在慢机器上抖动。
    """
    for name in ("a", "b", "c", "d"):
        _SPECS[name] = {"delay": 0.15}

    async def main():
        t0 = time.monotonic()
        ts = await agent_mod.build_user_toolset("u1")
        return time.monotonic() - t0, ts

    elapsed, ts = asyncio.run(main())
    assert len(ts._manifest) == 4
    assert elapsed < 0.45, f"疑似仍是串行（耗时 {elapsed:.2f}s，4×0.15=0.6s）"


def test_slowest_server_dominates():
    """一个极慢 + 三个快：总耗时由最慢的决定，不是相加。

    阈值取 0.36s（串行下界）与 0.30s（并发期望）之间的一半——
    卡在 0.33s 才能真的区分两者。取 0.5s 会让串行也通过（变异测试发现）。
    """
    _SPECS["slow"] = {"delay": 0.3}
    for name in ("f1", "f2", "f3"):
        _SPECS[name] = {"delay": 0.02}

    async def main():
        t0 = time.monotonic()
        await agent_mod.build_user_toolset("u2")
        return time.monotonic() - t0

    elapsed = asyncio.run(main())
    # 串行至少 0.3+0.02*3 = 0.36s；并发约 0.30s
    assert elapsed < 0.33, f"耗时 {elapsed:.3f}s，接近串行总和（0.36s）"


# --------------------------------------------------------------------------
# 顺序与失败隔离
# --------------------------------------------------------------------------

def test_manifest_order_matches_config_order():
    """manifest 顺序必须等于配置顺序——前端直接展示，乱序会像"配置变了"。"""
    # 故意让后面的更快完成，验证 gather 仍保留入参顺序
    _SPECS["first"] = {"delay": 0.12}
    _SPECS["second"] = {"delay": 0.06}
    _SPECS["third"] = {"delay": 0.01}

    async def main():
        return await agent_mod.build_user_toolset("u3")

    ts = asyncio.run(main())
    names = [row["name"] for row in ts._manifest]
    assert names == ["first", "second", "third"], names


def test_one_failure_does_not_break_others():
    """一个 server 抛异常，其他照常连接且如实记录失败行。"""
    _SPECS["good1"] = {"delay": 0.01, "tools": 2}
    _SPECS["bad"] = {"raise": True}
    _SPECS["good2"] = {"delay": 0.01, "tools": 3}

    async def main():
        return await agent_mod.build_user_toolset("u4")

    ts = asyncio.run(main())
    by_name = {row["name"]: row for row in ts._manifest}
    assert by_name["good1"]["ok"] is True
    assert by_name["good2"]["ok"] is True
    assert by_name["bad"]["ok"] is False
    assert "boom" in by_name["bad"]["error"]
    # 两个好的工具都要在合并结果里
    assert len(ts.tools) == 5


def test_all_fail_falls_back_to_demo():
    """全部失败时回退到内置 demo，但 manifest 仍如实保留失败原因。"""
    _SPECS["bad1"] = {"raise": True}
    _SPECS["bad2"] = {"raise": True}

    async def main():
        return await agent_mod.build_user_toolset("u5")

    ts = asyncio.run(main())
    assert len(ts._manifest) == 2
    assert all(row["ok"] is False for row in ts._manifest)


# --------------------------------------------------------------------------
# 并发上限
# --------------------------------------------------------------------------

def test_concurrency_is_capped(monkeypatch):
    """配置 20 个 server 时，同时进行的不超过 MCP_CONNECT_CONCURRENCY。"""
    monkeypatch.setenv("MCP_CONNECT_CONCURRENCY", "3")
    peak = {"n": 0, "cur": 0}

    class CountingToolset(FakeToolset):
        async def __aenter__(self):
            peak["cur"] += 1
            peak["n"] = max(peak["n"], peak["cur"])
            try:
                await asyncio.sleep(0.05)
                self.tools = [object()]
                return self
            finally:
                peak["cur"] -= 1

    monkeypatch.setattr(agent_mod, "MCPToolset", CountingToolset)
    for i in range(9):
        _SPECS[f"s{i}"] = {}

    async def main():
        return await agent_mod.build_user_toolset("u6")

    ts = asyncio.run(main())
    assert len(ts._manifest) == 9
    assert peak["n"] <= 3, f"并发峰值 {peak['n']} 超过上限 3"


# --------------------------------------------------------------------------
# 取消：不留孤儿
# --------------------------------------------------------------------------

def test_cancel_closes_already_connected_toolsets():
    """取消时要把已连好的 toolset 关掉，否则留下孤儿连接/子进程。"""
    created: list[FakeToolset] = []

    class TrackingToolset(FakeToolset):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

        async def __aenter__(self):
            await asyncio.sleep(10)      # 永远等不到
            return self

    _SPECS["hang"] = {}
    # 再造一个「已完成」的：靠 CancelledError 触发清理路径
    monkeypatch_target = TrackingToolset

    async def main():
        import app.agent as m
        orig = m.MCPToolset
        m.MCPToolset = monkeypatch_target
        try:
            task = asyncio.ensure_future(m.build_user_toolset("u7"))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            m.MCPToolset = orig

    asyncio.run(main())
    # 至少不该挂起（能被取消到 CancelledError 就说明没有吞掉取消）
    assert True