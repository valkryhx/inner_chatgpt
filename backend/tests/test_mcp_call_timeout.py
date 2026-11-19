"""MCP 工具调用必须有超时：连接半死时不能让整轮生成永久挂住。

## 缺陷现场（2026-11-19，多实例部署，真实用户会话）

用户让两个子代理搜「特朗普」和「普京」。两个子代理都发出 `tool_start`
（`web_search_exa` / `web_fetch_exa`），然后**永远没有 `tool_result`**：

- 父代理连等两轮 `wait_agent`（240s + 300s），两次都是 `timed_out: true`；
- 子代理 `turns=0`、侧链消息 0 条、`event_seq` 停在 143 不再推进；
- 实例进程仍是 `S (sleeping)`、`/api/health` 20ms 返回 —— 事件循环健康，
  是某个协程的 `await` 永不返回；
- 同一份代码、同一个 exa 地址，新建连接调用 **0.9s** 就成功。

## 根因

`app/mcp_client.py` 的 `_wrap._invoke` 里：

    result = await client.call_tool(name, payload)          # 裸 await
    ...
    result = await toolset._client.call_tool(name, payload) # 重连后又一次裸 await

而 SDK 的默认值就是「无超时」：

    fastmcp.Client.call_tool(..., timeout: ... | None = None)
    mcp.ClientSession.call_tool(..., read_timeout_seconds: ... | None = None)

对照：同仓 `runtime.py:402` 的探活**是**包了 `asyncio.wait_for(...)` 的，
它甚至把「超时」明确判定为「连接已死」。也就是说作者知道会卡，
但真正执行工具的那条路径没有这层保护。

## 为什么这在多实例下更容易炸

`UserRuntime` 是进程级缓存，MCP 连接要活很久。单实例时用户还能重启进程
自救；多实例下请求被粘到哪个实例是 nginx 决定的，同一个坏连接会被
反复命中，用户看到的就是「卡住」，且刷新页面只会重新 attach 到一个
仍然卡着的 turn_job。

## 修复后必须成立

1. `call_tool` 卡住时，`tool.call()` 在限定时间内返回**可读的错误文本**
   （不是抛异常、不是永久挂起）——模型能据此继续下一轮；
2. 超时时间可通过 `MCP_CALL_TIMEOUT_S` 覆盖，默认值合理（不是 0/None）；
3. 正常调用（未超时）行为完全不变；
4. 重连后的那次调用同样受超时保护（两条路径都要包住）。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import mcp_client


class HangingClient:
    """模拟「server 收到请求但永不回包」的半死连接。"""

    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, name, args):
        self.calls += 1
        await asyncio.Event().wait()  # 永久阻塞
        raise AssertionError("unreachable")


class FastClient:
    """正常连接，用来验证超时没有误伤正常路径。"""

    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, name, args):
        self.calls += 1
        return {"content": [{"type": "text", "text": "搜索结果：OK"}]}


def _make_toolset(client):
    """造一个只差 _client 的最小 toolset（不真连 server）。"""
    ts = mcp_client.MCPToolset(None, user_id="u_probe", server_name="probe")
    ts._client = client
    return ts


def _fake_raw_tool(name="web_search_exa"):
    class _Raw:
        pass

    r = _Raw()
    r.name = name
    r.description = "search"
    r.inputSchema = {"type": "object", "properties": {"query": {"type": "string"}}}
    return r


# ---------------------------------------------------------------- RED

def test_hanging_call_tool_returns_error_instead_of_blocking(monkeypatch):
    """卡住的 MCP 调用必须变成可读错误，而不是永久挂起。

    这是本次事故的直接回归测试：不给超时的话，下面这行会永远等下去。
    """
    monkeypatch.setenv("MCP_CALL_TIMEOUT_S", "1")
    client = HangingClient()
    ts = _make_toolset(client)
    tool = ts._wrap(_fake_raw_tool())

    t = time.monotonic()
    out = asyncio.run(tool.call({"query": "trump"}))
    elapsed = time.monotonic() - t

    assert elapsed < 5.0, f"调用没有被超时截断，耗时 {elapsed:.1f}s"
    assert isinstance(out, str)
    assert out.startswith("[工具执行错误]"), out[:200]
    assert "超时" in out, out[:200]
    assert client.calls == 1


def test_timeout_is_configurable(monkeypatch):
    """`MCP_CALL_TIMEOUT_S` 必须生效，否则线上没法按 server 调优。"""
    monkeypatch.setenv("MCP_CALL_TIMEOUT_S", "2.5")
    assert mcp_client.mcp_call_timeout_s() == pytest.approx(2.5, abs=1e-6)


def test_timeout_default_is_sane(monkeypatch):
    """默认值必须是正数且有上限语义，不能是 0 / None（那等于没有超时）。"""
    monkeypatch.delenv("MCP_CALL_TIMEOUT_S", raising=False)
    v = mcp_client.mcp_call_timeout_s()
    assert v is not None
    assert 10 <= v <= 3600, f"默认超时 {v} 不合理"


def test_illegal_timeout_falls_back_to_default(monkeypatch):
    """坏配置不能让工具彻底不可用，退回默认值。"""
    monkeypatch.setenv("MCP_CALL_TIMEOUT_S", "not-a-number")
    v = mcp_client.mcp_call_timeout_s()
    assert 10 <= v <= 3600


# ---------------------------------------------------------------- GREEN 侧

def test_normal_call_unaffected(monkeypatch):
    """加了超时后，正常调用必须原样返回内容。"""
    monkeypatch.setenv("MCP_CALL_TIMEOUT_S", "30")
    client = FastClient()
    ts = _make_toolset(client)
    tool = ts._wrap(_fake_raw_tool())

    out = asyncio.run(tool.call({"query": "trump"}))
    assert "搜索结果：OK" in out
    assert client.calls == 1


def test_reconnect_path_also_has_timeout(monkeypatch):
    """重连后的那一次调用也必须包超时（两条裸 await 都要修）。"""
    monkeypatch.setenv("MCP_CALL_TIMEOUT_S", "1")

    class ReconnectingClient:
        def __init__(self):
            self.calls = 0

        async def call_tool(self, name, args):
            self.calls += 1
            raise RuntimeError("Session terminated")

    client = ReconnectingClient()
    ts = _make_toolset(client)

    async def _fake_reconnect():
        # 重连「成功」，但新 client 同样卡死
        ts._client = HangingClient()
        return True

    ts.reconnect = _fake_reconnect
    tool = ts._wrap(_fake_raw_tool())

    t = time.monotonic()
    out = asyncio.run(tool.call({"query": "trump"}))
    elapsed = time.monotonic() - t

    assert elapsed < 5.0, f"重连路径没有超时保护，耗时 {elapsed:.1f}s"
    assert out.startswith("[工具执行错误]"), out[:200]
    assert "超时" in out, out[:200]