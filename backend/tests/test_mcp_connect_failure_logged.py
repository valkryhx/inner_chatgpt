"""MCP 连接失败必须留下日志。

## 为什么

2026-11-20 排查「runtime 段为什么 12 秒」时，日志里
`grep -c TimeoutError` = **0**，于是先去查端点可达性、查代理、查 DNS，
绕了很久。真因是 `agent.py:_connect_one` 的 except 分支**只把异常写进
manifest，不打日志**：

```python
except Exception as e:
    return name, None, {"name": name, "ok": False, "error": f"{type(e).__name__}: {e}", ...}
```

于是「tavily 每次冷连吃满 12s 超时、0 工具」这件事在日志里完全隐形，
只有 UI 上少一个搜索源能被察觉。

## 判据

不只看「有没有 warning 行」——那在两种状态下都可能非零（其他模块也在打日志）。
必须断言**这条日志带上了 server 名与耗时**，因为排查真正需要的是
「哪个 server、慢了多久」，而不是「有个东西失败了」。

## 反向确认

把 `logger.warning` 那行删掉，本文件必须变红。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent as agent_mod  # noqa: E402


class _SlowFailToolset:
    """连接时先睡 delay 秒再抛异常，模拟超时。"""

    def __init__(self, target=None, *, user_id="", server_name="", **_kw):
        self.server_name = server_name
        self.tools: list = []
        self._error = ""
        self._delay = 0.0
        self._exc: Exception | None = None

    async def __aenter__(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        return self

    async def __aexit__(self, *_a):
        return None

    def status(self) -> dict:
        return {"name": self.server_name, "ok": True, "tool_count": 0,
                "error": "", "tools": []}

    @classmethod
    def merge(cls, toolsets):
        m = cls(user_id="", server_name="merged")
        for ts in toolsets:
            m.tools.extend(ts.tools)
        return m


@pytest.fixture
def _setup(monkeypatch):
    specs: dict[str, dict] = {}

    async def fake_list(_uid):
        return [{"name": n, "transport": "http", "enabled": True,
                 "url": f"http://x/{n}"} for n in specs]

    orig_init = _SlowFailToolset.__init__

    def patched_init(self, target=None, *, user_id="", server_name="", **kw):
        orig_init(self, target, user_id=user_id, server_name=server_name, **kw)
        spec = specs.get(server_name, {})
        self._delay = float(spec.get("delay", 0.0))
        self._exc = spec.get("exc")

    monkeypatch.setattr(agent_mod, "MCPToolset", _SlowFailToolset)
    monkeypatch.setattr(_SlowFailToolset, "__init__", patched_init)
    monkeypatch.setattr(agent_mod.db, "list_mcp_servers", fake_list)
    return specs


def test_connect_failure_logs_server_name_and_elapsed(_setup, caplog):
    """一个 server 抛异常时，必须打一条带 name / elapsed / err 的 warning。"""
    _setup["brokensrv"] = {"exc": RuntimeError("session terminated")}
    _setup["ok" + "srv"] = {}          # 一个正常的，证明失败不影响别人

    with caplog.at_level(logging.WARNING, logger="app.agent"):
        asyncio.run(agent_mod.build_user_toolset("u1"))

    hits = [r for r in caplog.records
            if "brokensrv" in r.getMessage() and "MCP" in r.getMessage()]
    assert hits, (
        "连接失败的 server 没有留下任何带名字的日志。"
        f"实际日志：{[r.getMessage() for r in caplog.records]}")

    msg = hits[0].getMessage()
    assert "elapsed" in msg, f"日志缺耗时字段，无法定位「慢在哪」：{msg}"
    assert "RuntimeError" in msg, f"日志缺异常类型：{msg}"


def test_connect_timeout_logs_message(_setup, caplog):
    """超时（asyncio.TimeoutError）同样要留日志，不能静默。"""
    _setup["slowsrv"] = {"delay": 5.0}     # 远超下面压到 0.2s 的超时
    agent_mod.os.environ["MCP_CONNECT_TIMEOUT"] = "0.2"
    try:
        with caplog.at_level(logging.WARNING, logger="app.agent"):
            asyncio.run(agent_mod.build_user_toolset("u2"))
    finally:
        agent_mod.os.environ.pop("MCP_CONNECT_TIMEOUT", None)

    hits = [r for r in caplog.records if "slowsrv" in r.getMessage()]
    assert hits, (
        "超时被静默吞掉——这正是 2026-11-20 排查绕远路的根因。"
        f"实际日志：{[r.getMessage() for r in caplog.records]}")


def test_zero_tool_server_logs(_setup, caplog):
    """连上了但 0 工具（tavily 的实际形态）也要留日志。

    tavily 的失败形态不是抛异常，而是 `tools=0` 走到
    「该 server 没有暴露任何工具」分支 —— 这条同样不能静默。
    """
    _setup["emptysrv"] = {}      # 不抛、0 工具

    with caplog.at_level(logging.WARNING, logger="app.agent"):
        asyncio.run(agent_mod.build_user_toolset("u3"))

    hits = [r for r in caplog.records if "emptysrv" in r.getMessage()]
    assert hits, (
        "0 工具分支没有任何日志——tavily 每次超时后正是走到这里。"
        f"实际日志：{[r.getMessage() for r in caplog.records]}")
