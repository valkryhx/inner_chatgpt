"""MCP 状态软 TTL：只在状态真变化时重建 runtime。

背景：RuntimeHub 的 runtime 按 (user, model) 永久缓存，MCP 中途断开后界面上
会一直显示「已连接」。加定时重建又会反复重连 MCP、并可能打断正在跑的轮次。

因此约束是三条：
1. TTL 内直接返回缓存状态，不重新探测；
2. TTL 过期后探测一次，但**状态没变就不重建**（不能白白 close 掉 client）；
3. 用户有对话在跑时一律不重建，否则正在用的 toolset 会被关掉。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import runtime as rt
from app.runtime import RuntimeHub, _same_manifest


def run(coro):
    return asyncio.run(coro)


# 每个测试用独立用户，避免全量跑时命中别的测试残留的活跃 generation 状态。
_UID = "u_ttl_isolated"


@pytest.fixture(autouse=True)
def _clean_global_state():
    """清掉模块级 generation 注册表，防止跨测试污染。

    has_active_generation_for_user 读的是模块级 _stop_events / _generation_tasks /
    _generation_reservations；其他测试如果遗留了活跃项，会让这里误判
    「有对话在跑」而跳过探测。
    """
    rt._stop_events.clear()
    rt._generation_tasks.clear()
    rt._generation_reservations.clear()
    yield
    rt._stop_events.clear()
    rt._generation_tasks.clear()
    rt._generation_reservations.clear()


class FakeToolset:
    def __init__(self, manifest):
        self._manifest = manifest
        self.closed = False

    @property
    def manifest(self):
        return self._manifest

    async def __aexit__(self, *exc):
        self.closed = True


class FakeRuntime:
    def __init__(self, manifest):
        self.toolset = FakeToolset(manifest)
        self.registry = type("R", (), {"tools": []})()
        self.closed = False

    async def close(self):
        self.closed = True


def _manifest(ok=True, count=12, name="sqlbotmcp"):
    return [{"name": name, "ok": ok, "tool_count": count, "error": "", "tools": []}]


def _hub_with(monkeypatch, manifest, *, built_ago=0.0):
    hub = RuntimeHub()
    fake = FakeRuntime(manifest)
    key = (_UID, "m1")
    hub._runtimes[key] = fake
    hub._built_at[key] = time.monotonic() - built_ago
    return hub, fake, key


# ---------------- manifest 比较 ----------------
def test_same_manifest_ignores_order_and_error_text():
    a = [{"name": "a", "ok": True, "tool_count": 3, "error": ""},
         {"name": "b", "ok": False, "tool_count": 0, "error": "boom"}]
    b = [{"name": "b", "ok": False, "tool_count": 0, "error": "different text"},
         {"name": "a", "ok": True, "tool_count": 3, "error": ""}]
    assert _same_manifest(a, b)


def test_same_manifest_detects_status_change():
    before = _manifest(ok=True, count=12)
    after = _manifest(ok=False, count=0)
    assert not _same_manifest(before, after)


def test_same_manifest_detects_count_change():
    assert not _same_manifest(_manifest(count=12), _manifest(count=11))


# ---------------- TTL ----------------
def test_within_ttl_returns_cache_without_probing(monkeypatch):
    calls = []

    async def fake_probe(user_id):
        calls.append(user_id)
        return _manifest(ok=False, count=0)

    monkeypatch.setattr(rt, "probe_mcp_status", fake_probe)
    hub, fake, _ = _hub_with(monkeypatch, _manifest(ok=True), built_ago=0.0)

    out = run(hub.refresh_mcp_status(_UID, "m1"))
    assert calls == []            # 没探测
    assert out[0]["ok"] is True    # 仍是缓存值
    assert fake.closed is False


def test_expired_ttl_probes_and_skips_rebuild_when_unchanged(monkeypatch):
    calls = []

    async def fake_probe(user_id):
        calls.append(user_id)
        return _manifest(ok=True, count=12)   # 与缓存一致

    monkeypatch.setattr(rt, "probe_mcp_status", fake_probe)
    hub, fake, key = _hub_with(monkeypatch, _manifest(ok=True, count=12), built_ago=999)

    out = run(hub.refresh_mcp_status(_UID, "m1"))
    assert calls == [_UID]
    assert out[0]["ok"] is True
    # 关键：状态没变 → 不重建、不 close
    assert fake.closed is False
    assert hub._runtimes[key] is fake
    # 但要续期，避免每次读状态都探测
    assert time.monotonic() - hub._built_at[key] < 1.0


def test_expired_ttl_rebuilds_when_status_changed(monkeypatch):
    async def fake_probe(user_id):
        return _manifest(ok=False, count=0)

    monkeypatch.setattr(rt, "probe_mcp_status", fake_probe)
    hub, fake, key = _hub_with(monkeypatch, _manifest(ok=True, count=12), built_ago=999)

    built = {}

    async def fake_get_runtime(user_id, model_id=""):
        built["called"] = True
        fresh = FakeRuntime(_manifest(ok=False, count=0))
        hub._runtimes[key] = fresh
        hub._built_at[key] = time.monotonic()
        return fresh

    monkeypatch.setattr(hub, "get_user_runtime", fake_get_runtime)

    out = run(hub.refresh_mcp_status(_UID, "m1"))
    assert built.get("called") is True
    assert fake.closed is True          # 旧实例被关掉
    assert out[0]["ok"] is False


def test_skips_rebuild_while_generation_is_active(monkeypatch):
    calls = []

    async def fake_probe(user_id):
        calls.append(user_id)
        return _manifest(ok=False, count=0)

    monkeypatch.setattr(rt, "probe_mcp_status", fake_probe)
    monkeypatch.setattr(rt, "has_active_generation_for_user", lambda uid: True)

    hub, fake, key = _hub_with(monkeypatch, _manifest(ok=True, count=12), built_ago=999)

    out = run(hub.refresh_mcp_status(_UID, "m1"))
    assert calls == []                 # 连探测都不做，避免打断当前轮次
    assert fake.closed is False
    assert out[0]["ok"] is True


def test_unknown_runtime_returns_empty(monkeypatch):
    hub = RuntimeHub()
    assert run(hub.refresh_mcp_status("nobody", "m1")) == []


# ---------------- 时间戳生命周期 ----------------
def test_invalidate_clears_built_at(monkeypatch):
    hub, fake, key = _hub_with(monkeypatch, _manifest(), built_ago=0)
    run(hub.invalidate(_UID))
    assert key not in hub._runtimes
    assert key not in hub._built_at


def test_has_active_generation_for_user_scopes_by_owner():
    """活跃判定按 owner 隔离：别的用户的生成不能影响本用户的重建决策。"""
    key = ("u_owner", "s1", "g1")
    # 用 reservation 表达「刚接收、尚未开始迭代」的活跃窗口。
    rt._generation_reservations[key] = time.monotonic()
    try:
        assert rt.has_active_generation_for_user("u_owner") is True
        assert rt.has_active_generation_for_user("u_other") is False
    finally:
        rt._generation_reservations.pop(key, None)
