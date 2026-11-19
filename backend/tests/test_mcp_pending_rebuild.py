"""活跃生成期间的 MCP 软 TTL：不能「永远不重建」，也不能「打断正在跑的轮次」。

## 死角现场（2026-11-19 事故，见 docs/20261119-子代理卡死事故分析-MCP调用无超时.md §6.3）

`refresh_mcp_status` 原来的实现：

    if has_active_generation_for_user(user_id):
        return getattr(ur.toolset, "manifest", []) or []   # ← 连探测都不做

注释写的理由是「避免打断正在执行的那一轮」。这个本意是对的 —— `invalidate`
会 `ur.close()`，把正在用的 toolset 和 LLM client 一起关掉。

但它同时造成了三个后果：

1. **用户无法自救**。用户发现卡住了，打开设置页想看看 MCP 是不是断了，
   设置页调的正是这个函数 —— 返回的还是缓存的假「已连接」。
   这是本次事故里最恶劣的一点：唯一的诊断入口给出错误信息。
2. **永远不重建**。卡住的那一轮「恰好就是有生成在跑」，所以这个分支
   永远命中，代码永远走不到重建。
3. **连探测都不做**，比「探测但不重建」更保守 —— 而探测是只读的
   （`_toolset_is_live` 只调一个无副作用的探针工具），根本不会打断谁。

## 修法：探测可以，重建不行；重建排队到生成结束

- 有活跃生成时**仍然探测**（只读、不占 MCP 配额、不关任何东西）；
- 探测到连接已死 → **不立刻重建**（保住原意），但
  ① 返回的 manifest 如实标注「已断开」让用户看见，② 记一个待重建标记；
- 待重建标记在**下一次没有活跃生成时**的 `get_user_runtime` 里消费。

这样既不打断，也不会永远不重建。

## 修复后必须成立

1. 有活跃生成 + 连接已死 → 不 close toolset，但 manifest 标断开 + 设置待重建；
2. 有活跃生成 + 连接活着 → 不重建，续期即可（省掉每次进设置页都探测）；
3. 待重建已设置 + 没有活跃生成 → 下次 `get_user_runtime` 必须真的重建；
4. 待重建已设置 + 仍有活跃生成 → 继续不动（不打断）；
5. 原行为完全不变：无活跃生成时 TTL 到期探测，死了就立刻重建。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import runtime as rt
from app.runtime import RuntimeHub

_UID = "u_pending_isolated"


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


def _manifest(ok=True, count=12, name="exa"):
    return [{"name": name, "ok": ok, "tool_count": count, "error": "", "tools": []}]


def _hub_with(manifest, *, built_ago=999.0):
    hub = RuntimeHub()
    fake = FakeRuntime(manifest)
    key = (_UID, "m1")
    hub._runtimes[key] = fake
    hub._built_at[key] = time.monotonic() - built_ago
    return hub, fake, key


def _patch(monkeypatch, *, live: bool, active: bool, probe_manifest=None):
    """统一 patch 三个外部依赖，让测试只盯着被测分支。"""
    probed = []

    async def fake_live(toolset):
        probed.append("live")
        return live

    async def fake_probe(user_id):
        probed.append("probe")
        return probe_manifest if probe_manifest is not None else _manifest()

    monkeypatch.setattr(rt, "_toolset_is_live", fake_live)
    monkeypatch.setattr(rt, "probe_mcp_status", fake_probe)
    monkeypatch.setattr(rt, "has_active_generation_for_user", lambda uid: active)
    return probed


# ---------------- 有活跃生成 ----------------

def test_active_generation_dead_connection_is_reported_not_hidden(monkeypatch):
    """有生成在跑 + 连接死了 → 不能假装「已连接」，也不能关闭 toolset。

    这是本次修复的核心：用户打开设置页必须看到「已断开」，
    而不是缓存的假状态（事故里用户完全无法判断卡在哪）。
    """
    _patch(monkeypatch, live=False, active=True)
    hub, fake, key = _hub_with(_manifest(ok=True, count=12))

    out = run(hub.refresh_mcp_status(_UID, "m1"))

    assert fake.closed is False, "不能关掉正在跑的那一轮的 toolset"
    assert hub._runtimes[key] is fake, "不能替换 runtime"
    assert out, "必须返回 manifest，不能空"
    assert out[0]["ok"] is False, f"必须如实标注断开，实际 {out}"
    assert out[0]["error"], "要给用户一个可读原因"
    assert key in hub._pending_rebuild, "必须记下待重建，否则永远不重建"


def test_active_generation_live_connection_refreshes_ttl(monkeypatch):
    """有生成在跑 + 连接活着 → 只续期，不重建、不标记。"""
    _patch(monkeypatch, live=True, active=True)
    hub, fake, key = _hub_with(_manifest(ok=True, count=12))

    out = run(hub.refresh_mcp_status(_UID, "m1"))

    assert fake.closed is False
    assert out[0]["ok"] is True, "连接是活的，应原样返回缓存状态"
    assert key not in hub._pending_rebuild
    # 续期：避免用户每进一次设置页就探测一遍
    assert time.monotonic() - hub._built_at[key] < 1.0


def test_active_generation_does_not_run_config_probe(monkeypatch):
    """有生成在跑时不做**配置**探测（那会重建），但活性探测要做。"""
    probed = _patch(monkeypatch, live=True, active=True)
    hub, _fake, _key = _hub_with(_manifest())

    run(hub.refresh_mcp_status(_UID, "m1"))

    assert "live" in probed, "活性探测必须做"
    assert "probe" not in probed, "配置探测会触发重建，有生成时不能做"


# ---------------- 待重建的消费 ----------------

def _patch_build(monkeypatch, hub):
    """让 get_user_runtime 能真的重建出一个 fake，而不去连 MCP。"""
    made = []

    class BuildableRuntime(FakeRuntime):
        async def build(self, user_id, model_id=None):
            self.built = True
            return self

    def factory():
        r = BuildableRuntime(_manifest(ok=True, count=12))
        made.append(r)
        return r

    monkeypatch.setattr(rt, "UserRuntime", factory)
    monkeypatch.setattr(hub, "_watch_invalidate", lambda uid: asyncio.sleep(0))
    return made


def test_pending_rebuild_is_consumed_when_no_generation_active(monkeypatch):
    """生成结束后，下一次 get_user_runtime 必须真的把死连接换掉。"""
    _patch(monkeypatch, live=False, active=False)
    hub, fake, key = _hub_with(_manifest(ok=True, count=12))
    hub._pending_rebuild.add(key)          # 上一轮有生成时留下的标记
    made = _patch_build(monkeypatch, hub)

    monkeypatch.setattr(rt, "has_active_generation_for_user", lambda uid: False)
    ur = run(hub.get_user_runtime(_UID, "m1"))

    assert fake.closed is True, "旧的死 runtime 必须被关掉"
    assert ur is not fake, "必须换成新的 runtime"
    assert made, "应当重建过"
    assert key not in hub._pending_rebuild, "标记必须被消费掉"


def test_pending_rebuild_not_consumed_while_still_active(monkeypatch):
    """仍在跑的时候绝不能消费待重建标记（否则等于打断了）。"""
    _patch(monkeypatch, live=False, active=True)
    hub, fake, key = _hub_with(_manifest(ok=True, count=12))
    hub._pending_rebuild.add(key)

    monkeypatch.setattr(rt, "has_active_generation_for_user", lambda uid: True)
    ur = run(hub.get_user_runtime(_UID, "m1"))

    assert fake.closed is False
    assert ur is fake, "仍有生成在跑，必须原样返回旧 runtime"
    assert key in hub._pending_rebuild, "标记要留着，等生成结束再消费"


def test_pending_rebuild_does_not_apply_to_other_users(monkeypatch):
    """待重建标记按 (user, model) 隔离，不能误伤别人。"""
    _patch(monkeypatch, live=False, active=False)
    hub, _fake, key = _hub_with(_manifest())
    hub._pending_rebuild.add(key)

    other_key = ("u_someone_else", "m1")
    other = FakeRuntime(_manifest())
    hub._runtimes[other_key] = other
    hub._built_at[other_key] = time.monotonic()
    monkeypatch.setattr(rt, "has_active_generation_for_user", lambda uid: False)

    ur = run(hub.get_user_runtime("u_someone_else", "m1"))
    assert ur is other, "别的用户不该被重建"
    assert other.closed is False


# ---------------- 原行为不变 ----------------

def test_no_active_generation_dead_connection_rebuilds_immediately(monkeypatch):
    """无生成在跑 + 连接死了 → 立刻重建（原有行为，不能被改坏）。"""
    _patch(monkeypatch, live=False, active=False)
    hub, fake, key = _hub_with(_manifest(ok=True, count=12))
    made = _patch_build(monkeypatch, hub)

    out = run(hub.refresh_mcp_status(_UID, "m1"))

    assert fake.closed is True
    assert made, "应当重建"
    assert out[0]["ok"] is True, "重建后的新连接状态"


def test_no_active_generation_live_and_unchanged_just_renews(monkeypatch):
    """无生成 + 连接活 + 配置没变 → 只续期不重建（原有行为）。"""
    _patch(monkeypatch, live=True, active=False, probe_manifest=_manifest(ok=True, count=12))
    hub, fake, key = _hub_with(_manifest(ok=True, count=12))

    run(hub.refresh_mcp_status(_UID, "m1"))

    assert fake.closed is False
    assert hub._runtimes[key] is fake
    assert time.monotonic() - hub._built_at[key] < 1.0