"""第 7 步验收：停止级联 + TTL 回收 + 残留清扫。

设计文档 §6.5。三件事都必须锁死：

1. ``/stop`` 能把容器内进程杀掉（宿主 ``_kill_tree`` 管不到容器）；
2. ``reap_idle`` 只回收**空闲**容器，``in_flight > 0`` 的绝不碰；
3. 进程重启后 ``cleanup_orphans`` 能把上一次留下的容器清干净。

全部用假 runtime，不真起容器。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.sandbox import manager as sbx_manager
from app.sandbox import pool as sbx_pool
from app.sandbox import probe as sbx_probe
from app.sandbox.config import SandboxConfig


def run(coro):
    return asyncio.run(coro)


class FakeRuntime:
    """记录调用、不碰 Docker。"""

    def __init__(self):
        self.created: list[tuple[str, str]] = []
        self.destroyed: list[str] = []
        self.killall: list[str] = []
        self.alive: dict[str, bool] = {}

    def create(self, user_id: str, host_workspace: str) -> str:
        cid = f"cid_{user_id}"
        self.created.append((user_id, host_workspace))
        self.alive[cid] = True
        return cid

    def destroy(self, container_id: str) -> None:
        self.destroyed.append(container_id)
        self.alive[container_id] = False

    def is_alive(self, container_id: str) -> bool:
        return self.alive.get(container_id, False)

    def exec_shell_sync_killall(self, container_id: str) -> int:
        self.killall.append(container_id)
        return 1


def make_cfg(**kw) -> SandboxConfig:
    base = dict(mode="strict", idle_ttl_s=900, max_containers=4, image="img:test",
                mvp_users=("u_a", "u_b"))
    base.update(kw)
    return SandboxConfig(**base)


# ---------------- /stop 级联 ----------------


def test_stop_user_kills_container_processes():
    rt = FakeRuntime()
    p = sbx_pool.ContainerPool(cfg=make_cfg(), runtime=rt)
    mgr = sbx_manager.SandboxManager(cfg=make_cfg(), pool=p)

    run(p.acquire("u_a", "/host/u_a"))
    n = run(mgr.stop_user("u_a"))

    assert n == 1, "stop_user 应真的杀进程并返回动作数"
    assert rt.killall == ["cid_u_a"], "必须作用在 u_a 的容器上"


def test_stop_user_noop_when_no_container():
    rt = FakeRuntime()
    p = sbx_pool.ContainerPool(cfg=make_cfg(), runtime=rt)
    mgr = sbx_manager.SandboxManager(cfg=make_cfg(), pool=p)

    assert run(mgr.stop_user("u_a")) == 0
    assert rt.killall == [], "没有容器时不该产生任何动作"


def test_stop_user_does_not_destroy_container():
    """stop 只杀进程，容器保留复用 —— 否则每次 /stop 都要重建（~700ms）。"""
    rt = FakeRuntime()
    p = sbx_pool.ContainerPool(cfg=make_cfg(), runtime=rt)
    mgr = sbx_manager.SandboxManager(cfg=make_cfg(), pool=p)

    run(p.acquire("u_a", "/host/u_a"))
    run(mgr.stop_user("u_a"))
    assert rt.destroyed == [], "stop 不该销毁容器"


# ---------------- TTL 回收 ----------------


def test_reap_idle_destroys_expired():
    rt = FakeRuntime()
    p = sbx_pool.ContainerPool(cfg=make_cfg(idle_ttl_s=60), runtime=rt)
    entry = run(p.acquire("u_a", "/host/u_a"))
    run(p.release(entry))          # acquire 会置 in_flight=1，必须归还才算空闲
    # 把 last_used 拨到 2 倍 TTL 之前
    entry.last_used = time.monotonic() - 121

    n = run(p.reap_idle())
    assert n == 1
    assert rt.destroyed == ["cid_u_a"]


def test_reap_idle_keeps_fresh():
    rt = FakeRuntime()
    p = sbx_pool.ContainerPool(cfg=make_cfg(idle_ttl_s=900), runtime=rt)
    run(p.acquire("u_a", "/host/u_a"))

    assert run(p.reap_idle()) == 0
    assert rt.destroyed == []


def test_reap_idle_never_touches_in_flight():
    """最关键的一条：正在执行的容器即使超时也不能回收（§6.5 铁律）。"""
    rt = FakeRuntime()
    p = sbx_pool.ContainerPool(cfg=make_cfg(idle_ttl_s=60), runtime=rt)
    entry = run(p.acquire("u_a", "/host/u_a"))
    entry.last_used = time.monotonic() - 999
    entry.in_flight = 1  # 模拟执行中

    assert run(p.reap_idle()) == 0, "in_flight>0 的容器绝不能回收"
    assert rt.destroyed == []


def test_reap_idle_cleans_dead_entries():
    """容器已经被外部删掉（docker rm）时，池里也必须收敛。"""
    rt = FakeRuntime()
    p = sbx_pool.ContainerPool(cfg=make_cfg(idle_ttl_s=60), runtime=rt)
    entry = run(p.acquire("u_a", "/host/u_a"))
    run(p.release(entry))
    entry.last_used = time.monotonic() - 999
    rt.alive["cid_u_a"] = False  # 外部删掉了

    assert run(p.reap_idle()) == 1
    assert "u_a" not in p._entries


# ---------------- 残留清扫 ----------------


def test_cleanup_orphans_calls_docker(monkeypatch):
    """启动清扫：删掉沙箱容器，且**按 owner 收窄**（不碰别人的）。

    2026-11-18 改：原来只按 hlz.sandbox=1 过滤，扩容后会误删
    其它进程正在用的容器。现在必须带上 hlz.owner 过滤。
    """
    destroyed: list[str] = []

    class FakeContainer:
        def __init__(self, cid):
            self.id = cid

        def remove(self, force=False):
            destroyed.append(self.id)

    captured: dict = {}

    class FakeClient:
        class containers:
            @staticmethod
            def list(all=False, filters=None):
                captured["filters"] = filters
                return [FakeContainer("old_1"), FakeContainer("old_2")]

        def close(self):
            pass

    monkeypatch.setattr(sbx_probe, "_client", lambda: FakeClient())
    n = sbx_probe.cleanup_orphans()
    assert n == 2
    assert destroyed == ["old_1", "old_2"]

    labels = captured["filters"]["label"]
    assert f"{sbx_probe.SANDBOX_LABEL}=1" in labels, "丢了沙箱命名空间过滤"
    assert any("hlz.owner" in x for x in labels), (
        f"没有 owner 过滤，扩容后会误删别的进程的容器：{labels}")


def test_cleanup_orphans_swallows_errors(monkeypatch):
    """Docker 没起也不能让 lifespan 崩。"""
    def boom():
        raise RuntimeError("docker down")

    monkeypatch.setattr(sbx_probe, "_client", boom)
    assert sbx_probe.cleanup_orphans() == 0


# ---------------- 关闭 ----------------


def test_shutdown_destroys_all_and_stops_reaper():
    rt = FakeRuntime()
    p = sbx_pool.ContainerPool(cfg=make_cfg(), runtime=rt)
    mgr = sbx_manager.SandboxManager(cfg=make_cfg(), pool=p)

    run(p.acquire("u_a", "/host/u_a"))
    run(p.acquire("u_b", "/host/u_b"))

    n = run(mgr.shutdown())
    assert n == 2
    assert sorted(rt.destroyed) == ["cid_u_a", "cid_u_b"]
    assert p._entries == {}