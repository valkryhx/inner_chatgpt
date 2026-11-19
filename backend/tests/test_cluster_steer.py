"""跨实例插话队列。

## 事故场景

```
浏览器 -> LB -> 实例 B  POST /queue  (用户中途补一句)
                  B: 写进 B 内存的 SteerQueue
              生成跑在实例 A
                  A: drain 自己的内存队列 -> 空 -> 那条消息丢了
```

## 修法

push 侧写 Redis list，生成侧的实例有后台泵把它搬进本地队列。
`drain()` 一字未改 —— 它仍在同步注入回调里被调，不能 await。

## 反向确认

把 `get_queue` 里的 `_start_pump` 去掉，
`test_remote_push_reaches_local_drain` 必须变红。
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
    from app import cluster, steer_queue
    monkeypatch.delenv("HLZ_REDIS_URL", raising=False)
    cluster.reset_for_tests()
    steer_queue._queues.clear()
    steer_queue._pumps.clear()
    steer_queue._pending_writes.clear()
    yield
    cluster.reset_for_tests()
    steer_queue._queues.clear()
    steer_queue._pumps.clear()


@pytest.fixture()
def with_redis(monkeypatch):
    from app import cluster, steer_queue
    monkeypatch.setenv("HLZ_REDIS_URL", REDIS_URL)
    monkeypatch.setenv("HLZ_REDIS_PREFIX", f"hlzsq{uuid.uuid4().hex[:8]}")
    cluster.reset_for_tests()
    steer_queue._queues.clear()
    steer_queue._pumps.clear()
    steer_queue._pending_writes.clear()
    yield
    cluster.reset_for_tests()
    steer_queue._queues.clear()
    steer_queue._pumps.clear()


# ---------------------------------------------------------------- 单实例

def test_push_and_drain_without_redis(no_redis):
    """没 Redis 时 push/drain 必须与迁移前完全一致。"""
    from app.steer_queue import get_queue

    async def go():
        q = get_queue("u1", "s1", "g1")
        d = q.push("补充一句")
        got = q.drain()
        return d, got

    d, got = run(go())
    assert d == 1
    assert len(got) == 1
    assert got[0]["text"] == "补充一句"


def test_empty_text_rejected(no_redis):
    from app.steer_queue import get_queue

    async def go():
        q = get_queue("u1", "s1", "g2")
        return q.push("   ")

    assert run(go()) == 0


def test_queue_depth_cap(no_redis):
    """队列满了必须拒绝（防止客户端刷屏撑爆上下文）。"""
    from app.steer_queue import get_queue, MAX_QUEUE_DEPTH

    async def go():
        q = get_queue("u1", "s1", "g3")
        for i in range(MAX_QUEUE_DEPTH):
            assert q.push(f"msg{i}") > 0
        return q.push("overflow")

    assert run(go()) == 0


# ---------------------------------------------------------------- 跨实例

@needs_redis
def test_remote_push_reaches_local_drain(with_redis):
    """**核心**：在「另一个实例」入队，本实例 drain 必须拿到。

    模拟方式：直接往 Redis list 写一条（等价于实例 B 的 push），
    然后等本实例的后台泵把它搬进来。
    """
    from app.steer_queue import get_queue
    from app.cluster import steerbus

    async def go():
        q = get_queue("u1", "s1", "g_remote")
        # 模拟实例 B：只写 Redis
        item = {"id": "steer_x", "seq": 0, "text": "来自另一个实例",
                "source": "user", "priority": "next", "created_at": 1.0}
        ok = await steerbus.push("u1", "s1", "g_remote", item)
        assert ok is True
        # 泵间隔 200ms，给 3s
        for _ in range(30):
            got = q.drain()
            if got:
                return got
            await asyncio.sleep(0.1)
        return []

    got = run(go())
    assert got, "另一个实例入队的插话没有到达本实例"
    assert got[0]["text"] == "来自另一个实例"


@needs_redis
def test_local_push_goes_to_redis(with_redis):
    """本实例 push 必须写进 Redis（否则另一个实例永远收不到）。"""
    from app.steer_queue import get_queue
    from app.cluster import steerbus

    async def go():
        q = get_queue("u1", "s1", "g_push")
        q.push("本地补充")
        # push 在 Redis 模式下是后台写，必须等它落盘才能查深度
        from app.steer_queue import flush_pending
        await flush_pending()
        # 等 fire-and-forget 的写入完成
        for _ in range(20):
            n = await steerbus.depth("u1", "s1", "g_push")
            if n > 0:
                return n
            await asyncio.sleep(0.05)
        return 0

    assert run(go()) > 0


@needs_redis
def test_pump_moves_item_to_redis_to_local(with_redis):
    """push 后最终能 drain 出来（走完 Redis -> 泵 -> 本地 一整圈）。"""
    from app.steer_queue import get_queue

    async def go():
        q = get_queue("u1", "s1", "g_round")
        q.push("绕一圈回来")
        from app.steer_queue import flush_pending
        await flush_pending()
        for _ in range(40):
            got = q.drain()
            if got:
                return got
            await asyncio.sleep(0.1)
        return []

    got = run(go())
    assert got and got[0]["text"] == "绕一圈回来"


@needs_redis
def test_drop_queue_clears_redis(with_redis):
    """drop 之后 Redis 里不能留残留，否则同 id 复用会拿到旧插话。"""
    from app import cluster
    from app.steer_queue import get_queue, drop_queue
    from app.cluster import steerbus

    async def go():
        q = get_queue("u1", "s1", "g_drop")
        q.push("要丢掉的")
        from app.steer_queue import flush_pending
        await flush_pending()
        drop_queue("u1", "s1", "g_drop")
        await asyncio.sleep(0.5)
        return await steerbus.depth("u1", "s1", "g_drop")

    assert run(go()) == 0