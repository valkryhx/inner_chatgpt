"""TTL 刷新的「会话活性」判定：缓存连接已死时必须重建，而不是只看 manifest。

缺陷现场（2026-11-10 真实 e2e，work/mcp_stale）：
MCP server 重启后，缓存 runtime 里的 fastmcp client 报 `Session terminated`；
TTL 探测用**新**连接，返回「已连接、2 工具」，与缓存 manifest 一致 →
判「状态没变」→ 不重建 → 用户每轮调用都失败，只能重启后端。

修复后必须成立：
1. 缓存连接真实往返失败（传输类错误）→ 判死 → 重建 runtime；
2. 缓存连接只是业务报错（参数/工具自身异常）→ 判活 → 不重建；
3. 缓存连接卡死超时 → 判死 → 重建；
4. 探针只用无必填参数的工具（避免缺参误判）；
5. 没有可探的连接 → 判活（不误杀内置 demo）。
"""
from __future__ import annotations

import asyncio

import pytest

from app import runtime as rt
from app.runtime import RuntimeHub, _pick_liveness_probe, _toolset_is_live

_UID = "u_live_isolated"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_global_state():
    rt._stop_events.clear()
    rt._generation_tasks.clear()
    rt._generation_reservations.clear()
    yield
    rt._stop_events.clear()
    rt._generation_tasks.clear()
    rt._generation_reservations.clear()


class DeadClient:
    """模拟 server 重启后的僵死会话。"""

    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    async def call_tool(self, name, args):
        self.calls += 1
        raise self._exc


class OkClient:
    def __init__(self):
        self.calls = 0

    async def call_tool(self, name, args):
        self.calls += 1
        return {"ok": True}


class SlowClient:
    async def call_tool(self, name, args):
        await asyncio.sleep(30)
        return {"ok": True}


class FakeTool:
    def __init__(self, name, required=None):
        self.name = name
        self.input_schema = {"type": "object",
                             "properties": {k: {"type": "string"}
                                            for k in (required or [])}}
        if required:
            self.input_schema["required"] = list(required)


class FakeChild:
    def __init__(self, client, tools, name="s1"):
        self._client = client
        self.tools = tools
        self.server_name = name


class FakeToolset:
    def __init__(self, children):
        self._children = children
        self._manifest = [{"name": c.server_name, "ok": True, "tool_count": len(c.tools),
                           "error": "", "tools": [t.name for t in c.tools]}
                          for c in children]

    @property
    def manifest(self):
        return self._manifest


# ---------------- 1. 会话活性判定 ----------------
def test_dead_session_is_detected_as_not_live():
    child = FakeChild(DeadClient(RuntimeError("McpError: Session terminated")),
                      [FakeTool("ping")])
    assert run(_toolset_is_live(FakeToolset([child]))) is False


def test_business_error_keeps_session_live():
    """工具自身报错说明连接是通的，不该重建 runtime。"""
    child = FakeChild(DeadClient(ValueError("validation error: bad arg")),
                      [FakeTool("ping")])
    assert run(_toolset_is_live(FakeToolset([child]))) is True


def test_healthy_session_is_live():
    client = OkClient()
    child = FakeChild(client, [FakeTool("ping")])
    assert run(_toolset_is_live(FakeToolset([child]))) is True
    assert client.calls == 1


def test_without_toolset_is_live():
    assert run(_toolset_is_live(None)) is True


def test_toolset_without_client_is_live():
    """内置 demo 的 in-process 连接没有 _client，不能误判为死。"""
    class InProc:
        _client = None
        tools = [FakeTool("echo")]
        server_name = "demo"
    assert run(_toolset_is_live(FakeToolset([InProc()]))) is True


def test_hanging_session_times_out_and_is_not_live(monkeypatch):
    monkeypatch.setattr(rt, "_LIVENESS_TIMEOUT_S", 0.05)
    child = FakeChild(SlowClient(), [FakeTool("ping")])
    assert run(_toolset_is_live(FakeToolset([child]))) is False


# ---------------- 2. 探针选择 ----------------
def test_probe_prefers_tool_without_required_args():
    tool = _pick_liveness_probe(FakeChild(OkClient(), [
        FakeTool("metric_series", required=["metric_ids"]),
        FakeTool("ping"),
    ]))
    assert tool.name == "ping"


def test_probe_returns_none_when_all_tools_need_args():
    tool = _pick_liveness_probe(FakeChild(OkClient(), [
        FakeTool("run_sql", required=["sql"]),
    ]))
    assert tool is None


def test_probe_never_picks_side_effecting_tool():
    """探活不能有副作用：无参但名字不像健康/列举类的工具不能当探针。"""
    tool = _pick_liveness_probe(FakeChild(OkClient(), [
        FakeTool("reset_all_data"),
        FakeTool("send_alert"),
    ]))
    assert tool is None


def test_probe_picks_listing_tools():
    tool = _pick_liveness_probe(FakeChild(OkClient(), [FakeTool("zone_list")]))
    assert tool.name == "zone_list"


# ---------------- 3. refresh 集成 ----------------
class FakeRuntime:
    def __init__(self, toolset):
        self.toolset = toolset
        self.registry = type("R", (), {"tools": []})()
        self.closed = False

    async def close(self):
        self.closed = True


def _hub(toolset, *, built_ago=999.0):
    hub = RuntimeHub()
    fake = FakeRuntime(toolset)
    key = (_UID, "m1")
    hub._runtimes[key] = fake
    hub._built_at[key] = __import__("time").monotonic() - built_ago
    return hub, fake, key


def test_refresh_rebuilds_when_cached_connection_is_dead(monkeypatch):
    """核心回归：manifest 没变，但缓存连接已死 → 必须重建。"""
    dead = FakeToolset([FakeChild(DeadClient(RuntimeError("Session terminated")),
                                  [FakeTool("ping")])])
    hub, fake, key = _hub(dead)

    async def fake_probe(user_id):
        # 新连接探测：看起来完全正常，manifest 与缓存一致
        return dead.manifest

    monkeypatch.setattr(rt, "probe_mcp_status", fake_probe)

    rebuilt = {}

    async def fake_get_runtime(user_id, model_id=""):
        rebuilt["called"] = True
        fresh = FakeRuntime(FakeToolset([FakeChild(OkClient(), [FakeTool("ping")])]))
        hub._runtimes[key] = fresh
        hub._built_at[key] = __import__("time").monotonic()
        return fresh

    monkeypatch.setattr(hub, "get_user_runtime", fake_get_runtime)

    out = run(hub.refresh_mcp_status(_UID, "m1"))
    assert rebuilt.get("called") is True
    assert fake.closed is True          # 坏的 runtime 被关掉
    assert out[0]["ok"] is True


def test_refresh_keeps_runtime_when_connection_is_alive(monkeypatch):
    alive = FakeToolset([FakeChild(OkClient(), [FakeTool("ping")])])
    hub, fake, key = _hub(alive)
    monkeypatch.setattr(rt, "probe_mcp_status", lambda uid: _async(alive.manifest))

    out = run(hub.refresh_mcp_status(_UID, "m1"))
    assert fake.closed is False
    assert out[0]["ok"] is True


def test_refresh_still_skips_while_generation_active(monkeypatch):
    dead = FakeToolset([FakeChild(DeadClient(RuntimeError("Session terminated")),
                                  [FakeTool("ping")])])
    hub, fake, key = _hub(dead)
    monkeypatch.setattr(rt, "has_active_generation_for_user", lambda uid: True)
    out = run(hub.refresh_mcp_status(_UID, "m1"))
    assert fake.closed is False         # 有轮次在跑，绝不动
    assert out[0]["ok"] is True


def _async(value):
    async def _inner(*a, **kw):
        return value
    return _inner()
