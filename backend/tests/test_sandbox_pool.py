"""pool 的单测（设计文档 §8 第 3 步）。

用**假 runtime**，不真起容器 —— 这一层测的是注册表逻辑：
复用、TTL 回收、LRU 淘汰、in_flight 保护、销毁幂等。

真容器的行为由第 2 步的真机验证覆盖（`runtime` 已 9/9 通过）。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.sandbox.config import SandboxConfig
from app.sandbox.errors import SandboxExecError
from app.sandbox.pool import ContainerPool


class FakeRuntime:
    """记录所有调用的假 runtime。"""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.destroyed: list[str] = []
        self.alive: set[str] = set()
        self._seq = 0

    def create(self, user_id: str, host_workspace: str) -> str:
        self._seq += 1
        cid = f"cid_{self._seq}"
        self.created.append((user_id, host_workspace))
        self.alive.add(cid)
        return cid

    def destroy(self, container_id: str) -> None:
        self.destroyed.append(container_id)
        self.alive.discard(container_id)

    def is_alive(self, container_id: str) -> bool:
        return container_id in self.alive


def run(coro):
    return asyncio.run(coro)


def make_pool(max_containers: int = 4, ttl: int = 900) -> tuple[ContainerPool, FakeRuntime]:
    cfg = SandboxConfig(mode="strict", max_containers=max_containers, idle_ttl_s=ttl)
    fake = FakeRuntime()
    return ContainerPool(cfg=cfg, runtime=fake), fake  # type: ignore[arg-type]


# ---------------- 懒创建与复用 ----------------


def test_first_acquire_creates():
    pool, fake = make_pool()
    entry = run(pool.acquire("u_a", "/ws/a"))
    assert entry.container_id == "cid_1"
    assert fake.created == [("u_a", "/ws/a")]
    assert pool.size() == 1


def test_second_acquire_reuses_same_container():
    """同用户第二次必须复用 —— 这是「每用户一容器」的核心语义。"""
    pool, fake = make_pool()
    e1 = run(pool.acquire("u_a", "/ws/a"))
    run(pool.release(e1))
    e2 = run(pool.acquire("u_a", "/ws/a"))
    assert e1.container_id == e2.container_id
    assert len(fake.created) == 1, "不该创建第二个容器"


def test_different_users_get_different_containers():
    pool, fake = make_pool()
    e1 = run(pool.acquire("u_a", "/ws/a"))
    e2 = run(pool.acquire("u_b", "/ws/b"))
    assert e1.container_id != e2.container_id
    assert len(fake.created) == 2


def test_dead_container_is_recreated():
    """容器意外消失（人手 docker rm、宿主重启）时自动重建。"""
    pool, fake = make_pool()
    e1 = run(pool.acquire("u_a", "/ws/a"))
    fake.alive.discard(e1.container_id)  # 模拟容器被杀
    e2 = run(pool.acquire("u_a", "/ws/a"))
    assert e2.container_id != e1.container_id
    assert len(fake.created) == 2


# ---------------- in_flight 保护 ----------------


def test_release_decrements_in_flight():
    pool, _ = make_pool()
    e = run(pool.acquire("u_a", "/ws/a"))
    assert e.in_flight == 1
    run(pool.release(e))
    assert e.in_flight == 0


def test_release_never_goes_negative():
    pool, _ = make_pool()
    e = run(pool.acquire("u_a", "/ws/a"))
    run(pool.release(e))
    run(pool.release(e))
    run(pool.release(e))
    assert e.in_flight == 0


def test_busy_containers_are_never_evicted():
    """全部在用、且已到上限 -> 抛错，而不是杀掉正在跑的容器。"""
    pool, fake = make_pool(max_containers=2)
    e1 = run(pool.acquire("u_a", "/ws/a"))   # in_flight=1
    e2 = run(pool.acquire("u_b", "/ws/b"))   # in_flight=1
    with pytest.raises(SandboxExecError, match="全部正在使用"):
        run(pool.acquire("u_c", "/ws/c"))
    assert fake.destroyed == [], "正在使用的容器不能被淘汰"
    assert pool.size() == 2


# ---------------- LRU 淘汰 ----------------


def test_lru_evicts_least_recently_used_idle():
    pool, fake = make_pool(max_containers=2)
    e1 = run(pool.acquire("u_a", "/ws/a"))
    run(pool.release(e1))
    # 让 u_a 明显更旧
    e1.last_used = time.monotonic() - 3600

    e2 = run(pool.acquire("u_b", "/ws/b"))
    run(pool.release(e2))

    e3 = run(pool.acquire("u_c", "/ws/c"))   # 触发 LRU
    assert e3.container_id == "cid_3"
    assert "cid_1" in fake.destroyed, "最久未用的应被淘汰"
    assert "cid_2" not in fake.destroyed, "较新的不该被淘汰"
    assert pool.size() == 2


# ---------------- TTL 回收 ----------------


def test_reap_idle_destroys_expired():
    pool, fake = make_pool(ttl=10)
    e = run(pool.acquire("u_a", "/ws/a"))
    run(pool.release(e))
    e.last_used = time.monotonic() - 100     # 远超 TTL
    n = run(pool.reap_idle())
    assert n == 1
    assert e.container_id in fake.destroyed
    assert pool.size() == 0


def test_reap_idle_keeps_fresh_and_busy():
    pool, fake = make_pool(ttl=10)
    fresh = run(pool.acquire("u_a", "/ws/a"))
    run(pool.release(fresh))                  # 刚用完，未过期

    busy = run(pool.acquire("u_b", "/ws/b"))  # 在跑，且人为设为很旧
    busy.last_used = time.monotonic() - 1000

    n = run(pool.reap_idle())
    assert n == 0, "新鲜的和在跑的一律不回收"
    assert fake.destroyed == []


def test_reap_idle_on_empty_pool_is_noop():
    pool, _ = make_pool()
    assert run(pool.reap_idle()) == 0


# ---------------- 主动销毁 / shutdown ----------------


def test_destroy_user_removes_and_destroys():
    pool, fake = make_pool()
    e = run(pool.acquire("u_a", "/ws/a"))
    assert run(pool.destroy_user("u_a")) is True
    assert e.container_id in fake.destroyed
    assert pool.size() == 0


def test_destroy_user_unknown_returns_false():
    pool, _ = make_pool()
    assert run(pool.destroy_user("nobody")) is False


def test_shutdown_destroys_all():
    pool, fake = make_pool(max_containers=8)
    run(pool.acquire("u_a", "/ws/a"))
    run(pool.acquire("u_b", "/ws/b"))
    n = run(pool.shutdown())
    assert n == 2
    assert len(fake.destroyed) == 2
    assert pool.size() == 0


# ---------------- 并发安全 ----------------


def test_concurrent_acquire_same_user_creates_once():
    """两个协程同时要同一用户的容器，只能创建一个。

    这是「同用户多会话共享容器」语义的正确性前提：
    若并发下各建一个，会话 A 的进程会话 B 看不到，
    而它们本该共享同一个 workspace 视图。
    """
    pool, fake = make_pool()

    async def scenario():
        return await asyncio.gather(
            pool.acquire("u_a", "/ws/a"),
            pool.acquire("u_a", "/ws/a"),
        )

    e1, e2 = run(scenario())
    assert e1.container_id == e2.container_id
    assert len(fake.created) == 1, "并发下只该创建一次"
    assert e1.in_flight == 2, "两次 acquire 都要计数"