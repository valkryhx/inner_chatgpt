"""多实例协调层的测试。

## 分两类

1. **无 Redis（单实例降级）** —— 必须与迁移前行为完全一致。这类测试
   在任何机器上都能跑，是回归网。
2. **有 Redis** —— 需要 `HLZ_TEST_REDIS_URL`，验证真正的跨实例语义。

## 反向确认

锁的正确性靠三条：
- 释放要校验持有者（否则 A 的 finally 删掉 B 的锁）
- 续约要校验持有者（否则把别人的锁续上）
- 超时要抛 `LockTimeout`（否则调用方卡死）

`test_lock_release_does_not_steal_foreign_lock` 是最关键的一条：
把 `_RELEASE_LUA` 换成朴素 `DEL`，它必须立刻变红。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
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
    """明确的单实例环境。"""
    from app import cluster
    monkeypatch.delenv("HLZ_REDIS_URL", raising=False)
    cluster.reset_for_tests()
    cluster.lock.reset_for_tests()
    cluster.leader.reset_for_tests()
    yield
    cluster.reset_for_tests()
    cluster.lock.reset_for_tests()
    cluster.leader.reset_for_tests()


@pytest.fixture()
def with_redis(monkeypatch):
    from app import cluster
    monkeypatch.setenv("HLZ_REDIS_URL", REDIS_URL)
    monkeypatch.setenv("HLZ_REDIS_PREFIX", f"hltest{uuid.uuid4().hex[:8]}")
    monkeypatch.setenv("HLZ_LOCK_TTL_MS", "3000")
    monkeypatch.setenv("HLZ_LOCK_WAIT_MS", "3000")
    monkeypatch.setenv("HLZ_LEADER_TTL_MS", "3000")
    monkeypatch.setenv("HLZ_LEADER_REFRESH_MS", "800")
    cluster.reset_for_tests()
    cluster.lock.reset_for_tests()
    cluster.leader.reset_for_tests()
    yield
    cluster.reset_for_tests()
    cluster.lock.reset_for_tests()
    cluster.leader.reset_for_tests()


# ---------------------------------------------------------------- 配置

def test_cluster_disabled_without_url(no_redis):
    from app import cluster
    assert cluster.cluster_enabled() is False
    assert cluster.redis_url() is None


def test_cluster_enabled_with_url(monkeypatch):
    from app import cluster
    monkeypatch.setenv("HLZ_REDIS_URL", "redis://localhost:6379/0")
    cluster.reset_for_tests()
    assert cluster.cluster_enabled() is True


def test_lock_ttl_clamped(monkeypatch):
    """非法配置不能把 ttl 设成 0 或天文数字。"""
    from app.cluster import config as ccfg
    monkeypatch.setenv("HLZ_LOCK_TTL_MS", "1")
    assert ccfg.lock_ttl_ms() == 1000          # 下限
    monkeypatch.setenv("HLZ_LOCK_TTL_MS", "999999999")
    assert ccfg.lock_ttl_ms() == 300000        # 上限
    monkeypatch.setenv("HLZ_LOCK_TTL_MS", "not-a-number")
    assert ccfg.lock_ttl_ms() == 10000         # 默认值


def test_leader_refresh_smaller_than_ttl(monkeypatch):
    """续约间隔必须小于租期，否则一定丢锁。"""
    from app.cluster import config as ccfg
    monkeypatch.setenv("HLZ_LEADER_TTL_MS", "6000")
    monkeypatch.delenv("HLZ_LEADER_REFRESH_MS", raising=False)
    assert ccfg.leader_refresh_ms() < ccfg.leader_ttl_ms()


# ---------------------------------------------------------------- 单实例降级

def test_local_lock_mutual_exclusion(no_redis):
    """没有 Redis 时锁仍然互斥（进程内）。"""
    from app import cluster

    order: list[str] = []

    async def worker(tag: str, hold: float):
        async with await cluster.distributed_lock("t1"):
            order.append(f"{tag}-in")
            await asyncio.sleep(hold)
            order.append(f"{tag}-out")

    async def go():
        await asyncio.gather(worker("a", 0.05), worker("b", 0.01))

    run(go())
    # 必须成对出现，不能交错
    assert order[0].endswith("-in") and order[1].endswith("-out")


def test_local_lock_timeout(no_redis, monkeypatch):
    """拿不到锁必须抛 LockTimeout，不能无限等。"""
    from app import cluster
    monkeypatch.setenv("HLZ_LOCK_WAIT_MS", "200")

    async def go():
        held = await cluster.distributed_lock("t2")
        try:
            with pytest.raises(cluster.lock.LockTimeout):
                await cluster.distributed_lock("t2")
        finally:
            await held.release()

    run(go())


def test_local_lock_released_on_exception(no_redis):
    """异常路径也必须释放，否则下一次永久卡住。"""
    from app import cluster

    async def go():
        with pytest.raises(ValueError):
            async with await cluster.distributed_lock("t3"):
                raise ValueError("boom")
        # 能再次拿到就说明释放了
        async with await cluster.distributed_lock("t3"):
            return True

    assert run(go()) is True


def test_leader_is_true_without_redis(no_redis):
    """单实例下永远是 leader（本来就没有别人）。"""
    from app import cluster
    assert cluster.get_elector().is_leader is True


def test_leader_callback_fires_once_without_redis(no_redis):
    from app import cluster
    calls: list[str] = []

    async def cb():
        calls.append("x")

    async def go():
        e = cluster.get_elector()
        e.on_become_leader("startup", cb)
        await e.start()
        await e.start()      # 幂等
        await e.stop()

    run(go())
    assert calls == ["x"]


# ---------------------------------------------------------------- Redis 集成

@needs_redis
def test_redis_lock_blocks_second_holder(with_redis):
    """真 Redis：第二个持有者必须等到第一个释放。"""
    from app import cluster

    marks: list[str] = []

    async def go():
        a = await cluster.distributed_lock("rl1", wait_ms=5000)
        marks.append("a-in")

        async def other():
            b = await cluster.distributed_lock("rl1", wait_ms=5000)
            marks.append("b-in")
            await b.release()

        task = asyncio.create_task(other())
        await asyncio.sleep(0.3)
        assert "b-in" not in marks, "第二个持有者不该在锁未释放时进入"
        await a.release()
        await asyncio.wait_for(task, timeout=5)
        marks.append("done")

    run(go())
    assert marks == ["a-in", "b-in", "done"]


@needs_redis
def test_redis_lock_timeout(with_redis):
    from app import cluster

    async def go():
        a = await cluster.distributed_lock("rl2", wait_ms=5000)
        try:
            with pytest.raises(cluster.lock.LockTimeout):
                await cluster.distributed_lock("rl2", wait_ms=300)
        finally:
            await a.release()

    run(go())


@needs_redis
def test_lock_release_does_not_steal_foreign_lock(with_redis):
    """**最关键的一条**：不能用朴素 DEL 释放。

    场景：A 的锁过期了，B 抢到同一个 key；此时 A 的 finally 才跑到释放。
    如果释放是 `DEL key`，就会把 B 刚拿到的锁删掉 —— B 以为自己持锁，
    实际上已经没有任何保护。

    把 `_RELEASE_LUA` 换成 `DEL`，这条必须变红。
    """
    from app import cluster
    from app.cluster import lock as lockmod

    async def go():
        redis = await cluster.get_redis()
        prefix = os.environ["HLZ_REDIS_PREFIX"]
        key = f"{prefix}:lock:steal_test"

        # 模拟：B 持有锁
        await redis.set(key, "token-of-B", px=5000)

        # 模拟：A 用**自己的 token** 去释放（A 的锁早已过期）
        handle = cluster.lock.LockHandle(key, "token-of-A", 3000, redis)
        await handle.release()

        # B 的锁必须还在
        return await redis.get(key), await redis.pttl(key)

    value, ttl = run(go())
    assert value == "token-of-B", "A 释放时把 B 的锁删掉了（用了朴素 DEL？）"
    assert ttl > 0


@needs_redis
def test_lock_renew_keeps_alive_beyond_ttl(with_redis, monkeypatch):
    """续约必须真的生效：持有时间超过 ttl 后锁仍属于自己。"""
    from app import cluster
    monkeypatch.setenv("HLZ_LOCK_TTL_MS", "1000")

    async def go():
        h = await cluster.distributed_lock("renew1", ttl_ms=1000, wait_ms=3000)
        try:
            await asyncio.sleep(2.0)          # 远超 ttl
            assert h.alive is True, "锁已被回收，续约没起作用"
            # 别人此时拿不到
            with pytest.raises(cluster.lock.LockTimeout):
                await cluster.distributed_lock("renew1", ttl_ms=1000, wait_ms=200)
        finally:
            await h.release()

    run(go())


@needs_redis
def test_lock_lost_sets_alive_false(with_redis):
    """锁被别人拿走时，续约协程必须把 alive 置 False。"""
    from app import cluster

    async def go():
        h = await cluster.distributed_lock("lost1", ttl_ms=1000, wait_ms=3000)
        try:
            redis = await cluster.get_redis()
            # 强行改成别人的 token，模拟超期被抢
            await redis.set(h.key, "someone-else", px=5000)
            # 续约间隔是 ttl/3 ≈ 0.33s，等两轮
            await asyncio.sleep(1.2)
            return h.alive
        finally:
            await h.release()

    assert run(go()) is False


@needs_redis
def test_leader_only_one_wins(with_redis):
    """两个 elector 抢同一个 name，只能有一个当 leader。"""
    from app import cluster

    async def go():
        e1 = cluster.LeaderElector("test-elect")
        e2 = cluster.LeaderElector("test-elect")
        fired: list[str] = []

        async def cb1():
            fired.append("e1")

        async def cb2():
            fired.append("e2")

        e1.on_become_leader("k", cb1)
        e2.on_become_leader("k", cb2)
        await e1.start()
        await e2.start()
        await asyncio.sleep(0.6)
        r = (e1.is_leader, e2.is_leader)
        await e1.stop()
        await e2.stop()
        return r, fired

    (l1, l2), fired = run(go())
    assert l1 != l2, "两个 elector 同时成为 leader"
    assert len(fired) == 1, f"回调应只跑一次，实际 {fired}"


@needs_redis
def test_leader_takes_over_after_expiry(with_redis, monkeypatch):
    """leader 停掉后，另一个实例必须能接手（租约释放）。"""
    from app import cluster
    monkeypatch.setenv("HLZ_LEADER_TTL_MS", "1500")
    monkeypatch.setenv("HLZ_LEADER_REFRESH_MS", "500")

    async def go():
        e1 = cluster.LeaderElector("takeover")
        await e1.start()
        await asyncio.sleep(0.4)
        assert e1.is_leader is True

        await e1.stop()             # stop 会主动释放租约

        e2 = cluster.LeaderElector("takeover")
        await e2.start()
        await asyncio.sleep(0.8)
        ok = e2.is_leader
        await e2.stop()
        return ok

    assert run(go()) is True