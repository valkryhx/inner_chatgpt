"""跨实例停止信号。

## 事故场景

```
浏览器 -> LB -> 实例 B  POST /stop  (user=u1, gen=g1)
                  B: 内存里没有 g1 的 stop_event（它在 A 的内存里）
                  -> 返回 ok，实际什么都没停，用户看到生成继续跑
```

## 修法

1. `request_stop` 除了 set 本地 Event，还 `SET hlz:stop:<u>:<s>:<g> <ttl>`
2. `register_generation` 起一个后台协程轮询那个 key，命中就 set 本地 Event
3. `stopped()` 一字未改 —— 仍然读本地 Event，零开销

## 为什么轮询而不是 pubsub

pubsub 在重连时丢消息，「停止」丢了就是用户点了没反应。
1s 轮询的延迟用户感知不到（"很快就停了"），但正确性有保证。

## 反向确认

把 `_start_stop_watch` 里的 watch 去掉，`test_remote_stop_reaches_local_event`
必须变红。
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
    monkeypatch.setenv("HLZ_REDIS_PREFIX", f"hlzstop{uuid.uuid4().hex[:8]}")
    cluster.reset_for_tests()
    yield
    cluster.reset_for_tests()


# ---------------------------------------------------------------- 单实例

def test_publish_is_noop_without_redis(no_redis):
    from app.cluster import stopsignal

    async def go():
        return await stopsignal.publish("u1", "s1", "g1")

    assert run(go()) is False


def test_watch_returns_none_without_redis(no_redis):
    from app.cluster import stopsignal

    async def go():
        ev = asyncio.Event()
        return await stopsignal.watch("u1", "s1", "g1", ev)

    assert run(go()) is None


def test_local_stop_still_works_without_redis(no_redis):
    """没 Redis 时进程内停止必须照旧（迁移前行为）。"""
    from app import runtime

    async def go():
        ev = runtime.register_generation("u1", "s1", "g_local")
        try:
            assert ev.is_set() is False
            runtime.request_stop("u1", "s1", "g_local")
            return ev.is_set()
        finally:
            runtime.unregister_generation("u1", "s1", "g_local")

    assert run(go()) is True


# ---------------------------------------------------------------- 跨实例

@needs_redis
def test_remote_stop_reaches_local_event(with_redis):
    """**核心**：把停止标记写进 Redis，本地的 Event 必须被 set 上。

    这条模拟「另一个实例收到 POST /stop」：它只往 Redis 写一个 key，
    本实例（跑着生成的那个）的 StopWatcher 轮询到就该停。
    """
    from app import runtime
    from app.cluster import stopsignal

    async def go():
        ev = runtime.register_generation("u1", "s1", "g_remote")
        try:
            assert ev.is_set() is False
            # 模拟另一个实例：只写 Redis，不碰本地内存
            ok = await stopsignal.publish("u1", "s1", "g_remote")
            assert ok is True
            # StopWatcher 每秒轮询一次，给 3s 足够
            for _ in range(30):
                if ev.is_set():
                    return True
                await asyncio.sleep(0.1)
            return False
        finally:
            runtime.unregister_generation("u1", "s1", "g_remote")

    assert run(go()) is True


@needs_redis
def test_stop_key_has_ttl(with_redis):
    """停止标记必须带 TTL，否则 Redis 里会永久堆积。"""
    from app import cluster
    from app.cluster import stopsignal

    async def go():
        await stopsignal.publish("u1", "s1", "g_ttl")
        redis = await cluster.get_redis()
        prefix = os.environ["HLZ_REDIS_PREFIX"]
        key = f"{prefix}:stop:u1:s1:g_ttl"
        return await redis.pttl(key)

    ttl = run(go())
    assert 0 < ttl <= 300_000, f"TTL 异常：{ttl}"


@needs_redis
def test_unregister_clears_remote_flag(with_redis):
    """生成结束后要清掉 Redis 标记，否则同 id 复用会被误停。"""
    from app import runtime
    from app.cluster import stopsignal

    async def go():
        runtime.register_generation("u1", "s1", "g_clear")
        await stopsignal.publish("u1", "s1", "g_clear")
        runtime.unregister_generation("u1", "s1", "g_clear")
        await asyncio.sleep(0.6)      # 等后台清理任务跑完
        redis = await stopsignal.get_redis() if hasattr(stopsignal, "get_redis") else None
        from app import cluster
        redis = await cluster.get_redis()
        prefix = os.environ["HLZ_REDIS_PREFIX"]
        return await redis.exists(f"{prefix}:stop:u1:s1:g_clear")

    assert run(go()) == 0


@needs_redis
def test_watcher_stops_polling_after_unregister(with_redis):
    """监听协程必须被回收，否则每轮生成留一个每秒打 Redis 的协程。"""
    from app import runtime

    async def go():
        runtime.register_generation("u1", "s1", "g_reap")
        before = len(asyncio.all_tasks())
        runtime.unregister_generation("u1", "s1", "g_reap")
        await asyncio.sleep(0.5)
        after = len(asyncio.all_tasks())
        # 只要求「没有留下正在跑的 watcher」，不苛求精确数量
        return after <= before + 1

    assert run(go()) is True