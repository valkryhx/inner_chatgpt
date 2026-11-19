"""跨实例 runtime 缓存失效（MCP 配置在多实例下的真实痛点）。

## 问题

`RuntimeHub._runtimes` 缓存 `UserRuntime`，里面有：
- **进程内的 MCP 连接**（fastmcp Client，持 socket / 子进程）
- 系统提示词（构建时烘焙了 skills 索引）

用户在实例 B 改了 MCP 配置 -> `hub.invalidate(user_id)` **只清 B 的**。
实例 A 手里的旧连接会继续用，直到 `MCP_STATUS_TTL_S`（60s）到期后的
状态比对才可能发现 —— 即「改了配置最多一分钟才生效，而且可能不生效」。

MCP 连接跨进程无法共享（socket / stdio 都是进程内资源），
所以只能"各自失效重建"。这就是本模块要做的事。

## 为什么 Hlaudz 不需要 LibreChat 那套 lease

LibreChat 的 `FlowStateManager.acquireLease` 保护的是 **OAuth refresh
token 的并发重放**（RFC 9700 §4.13.2：重放会让整个授权族被吊销）。
Hlaudz 的 MCP headers 是静态配置（`mcp_config.py`），没有 refresh flow，
所以不存在这类风险，需要的只是"让别的实例知道该重建缓存了"。

## 反向确认

把 `_watch_invalidate` 里的 `watcher.start()` 删掉，
`test_remote_publish_reaches_local_invalidate` 必须变红。
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

REDIS_URL = os.environ.get("HLZ_TEST_REDIS_URL", "")

needs_redis = pytest.mark.skipif(
    not REDIS_URL, reason="未设置 HLZ_TEST_REDIS_URL，跳过 Redis 集成测试")


def run(coro):
    return asyncio.run(coro)


class FakeRuntime:
    """占位 runtime：invalidate 会调 close()，所以要有一个。"""

    closed = False

    async def close(self):
        self.closed = True


@pytest.fixture()
def no_redis(monkeypatch):
    from app import cluster
    monkeypatch.delenv("HLZ_REDIS_URL", raising=False)
    cluster.reset_for_tests()
    yield
    cluster.reset_for_tests()


@pytest.fixture()
def with_redis(monkeypatch):
    from app import cluster
    monkeypatch.setenv("HLZ_REDIS_URL", REDIS_URL)
    monkeypatch.setenv("HLZ_REDIS_PREFIX", f"hlzinv{uuid.uuid4().hex[:8]}")
    cluster.reset_for_tests()
    yield
    cluster.reset_for_tests()


# ---------------------------------------------------------------- 单实例

def test_publish_is_noop_without_redis(no_redis):
    from app.cluster import invalidate

    async def go():
        return await invalidate.publish("u1")

    assert run(go()) is False


def test_version_is_none_without_redis(no_redis):
    from app.cluster import invalidate

    async def go():
        return await invalidate.current_version("u1")

    assert run(go()) is None


def test_invalidate_cluster_still_clears_locally_without_redis(no_redis):
    """没 Redis 时 `invalidate_cluster` 必须照常清本地缓存。"""
    from app.runtime import RuntimeHub

    async def go():
        hub = RuntimeHub()
        # 塞一个假 runtime（不 build，避免真的连 MCP）
        hub._runtimes[("u1", "")] = FakeRuntime()
        await hub.invalidate_cluster("u1")
        return ("u1", "") in hub._runtimes

    assert run(go()) is False


# ---------------------------------------------------------------- 跨实例

@needs_redis
def test_publish_increments_version(with_redis):
    """每次 publish 版本号都要变 —— 订阅方靠它发现「有变更」。"""
    from app.cluster import invalidate

    async def go():
        await invalidate.publish("u1")
        v1 = await invalidate.current_version("u1")
        await invalidate.publish("u1")
        v2 = await invalidate.current_version("u1")
        return v1, v2

    v1, v2 = run(go())
    assert v1 is not None and v2 is not None
    assert v2 > v1, f"版本号没递增：{v1} -> {v2}"


@needs_redis
def test_remote_publish_reaches_local_invalidate(with_redis):
    """**核心**：另一个实例广播失效，本实例的缓存必须被清。

    模拟方式：本实例有缓存 + watcher 在跑；然后直接 publish（等价于
    另一个实例改了配置），等 watcher 轮询到就应清掉本地缓存。
    """
    from app.runtime import RuntimeHub
    from app.cluster import invalidate

    async def go():
        hub = RuntimeHub()
        hub._runtimes[("u1", "")] = FakeRuntime()
        await hub._watch_invalidate("u1")
        assert ("u1", "") in hub._runtimes

        # 模拟「实例 B 改了配置」
        await invalidate.publish("u1")

        # 轮询间隔 2s，给 8s
        for _ in range(80):
            if ("u1", "") not in hub._runtimes:
                return True
            await asyncio.sleep(0.1)
        return False

    assert run(go()) is True, "另一个实例的失效广播没有清掉本地缓存"


@needs_redis
def test_watcher_does_not_fire_on_startup(with_redis):
    """进程启动后的第一次读取只是建立基线，不该白清一次缓存。"""
    from app.runtime import RuntimeHub
    from app.cluster import invalidate

    async def go():
        # 先有一个非零版本（模拟历史上已有变更）
        await invalidate.publish("u2")
        await invalidate.publish("u2")

        hub = RuntimeHub()
        hub._runtimes[("u2", "")] = FakeRuntime()
        await hub._watch_invalidate("u2")
        # 等几个轮询周期
        await asyncio.sleep(5)
        return ("u2", "") in hub._runtimes

    assert run(go()) is True, "启动时把已有版本误判成变更，白清了缓存"


@needs_redis
def test_invalidate_cluster_publishes(with_redis):
    """`invalidate_cluster` 必须同时清本地 + 广播。"""
    from app.runtime import RuntimeHub
    from app.cluster import invalidate

    async def go():
        hub = RuntimeHub()
        hub._runtimes[("u3", "")] = FakeRuntime()
        before = await invalidate.current_version("u3")
        await hub.invalidate_cluster("u3")
        after = await invalidate.current_version("u3")
        return ("u3", "") in hub._runtimes, before, after

    still, before, after = run(go())
    assert still is False, "没有清本地缓存"
    assert after is not None and after != before, "没有广播"


def test_me_and_skills_use_cluster_invalidate():
    """配置变更的调用点必须用跨实例版本，否则多实例下改了不生效。"""
    import inspect
    from app.routers import me, skills

    me_src = inspect.getsource(me)
    skills_src = inspect.getsource(skills)
    assert "invalidate_cluster" in me_src, "me.py 还在用只清本地的 invalidate"
    assert "invalidate_cluster" in skills_src, "skills.py 还在用只清本地的 invalidate"


def test_refresh_mcp_status_still_uses_local_invalidate():
    """`refresh_mcp_status` 内部重建是**本进程**的事，不该广播。

    广播会形成「A 广播 -> B 清 -> B 重建 -> B 广播 -> A 清」的环。
    """
    import inspect
    from app import runtime

    src = inspect.getsource(runtime.RuntimeHub.refresh_mcp_status)
    assert "await self.invalidate(" in src
    assert "invalidate_cluster" not in src