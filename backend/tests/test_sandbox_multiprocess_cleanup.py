"""多实例下的容器清扫语义。

## 要防的事故

```
t=0  实例 A 启动，建容器 hlz_sbx_u1（owner = host:111）
t=5  实例 B 启动，清扫 same_machine
     -> A 的容器 owner 以 "host:" 开头 -> 被判定为本机残留 -> 删除
     -> A 正在这个容器里跑用户代码 -> 炸
```

只看「机器名」不够，必须知道**哪些 owner 还活着**。

## 三层保护（都要测）

1. 没有 Redis：按机器名清（单实例语义，与迁移前一致）
2. 有 Redis + 存活列表：跳过存活实例的容器
3. 有 Redis + 实例已死（心跳过期）：正常清理
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.sandbox import probe as sbx_probe  # noqa: E402
from app.sandbox.identity import owner_machine  # noqa: E402


class FakeContainer:
    def __init__(self, cid: str, owner: str) -> None:
        self.id = cid
        self.name = cid
        self.labels = {sbx_probe.OWNER_LABEL: owner}
        self.removed = False

    def remove(self, force: bool = False) -> None:
        self.removed = True


def _patch(monkeypatch, containers):
    class FakeClient:
        class containers:
            @staticmethod
            def list(all=False, filters=None):
                return containers

        def close(self):
            pass

    monkeypatch.setattr(sbx_probe, "_client", lambda: FakeClient())
    return containers


# ---------------------------------------------------------------- 单实例

def test_single_instance_clears_all_own_machine(monkeypatch):
    """没 Redis：本机所有沙箱容器都是残留（迁移前的行为）。"""
    host = owner_machine()
    cs = _patch(monkeypatch, [
        FakeContainer("c1", f"{host}:111"),
        FakeContainer("c2", f"{host}:222"),
        FakeContainer("c3", "other-host:333"),
    ])
    n = sbx_probe.cleanup_orphans(same_machine=True)   # live_owners=None
    assert n == 2
    assert [c.removed for c in cs] == [True, True, False]


# ---------------------------------------------------------------- 多实例

def test_skips_live_instance_containers(monkeypatch):
    """**核心**：正在干活的实例的容器绝不能删。"""
    host = owner_machine()
    live = {f"{host}:111"}
    cs = _patch(monkeypatch, [
        FakeContainer("alive", f"{host}:111"),      # 活着的实例 -> 保留
        FakeContainer("dead", f"{host}:999"),       # 死了的 -> 清理
    ])
    n = sbx_probe.cleanup_orphans(same_machine=True, live_owners=live)
    assert n == 1
    assert cs[0].removed is False, "删了存活实例正在用的容器"
    assert cs[1].removed is True


def test_all_live_clears_nothing(monkeypatch):
    host = owner_machine()
    live = {f"{host}:111", f"{host}:222"}
    cs = _patch(monkeypatch, [
        FakeContainer("a", f"{host}:111"),
        FakeContainer("b", f"{host}:222"),
    ])
    n = sbx_probe.cleanup_orphans(same_machine=True, live_owners=live)
    assert n == 0
    assert not any(c.removed for c in cs)


def test_empty_live_set_still_clears(monkeypatch):
    """存活集合为空（全部已死 / 没配 Redis）不应阻止清理。"""
    host = owner_machine()
    cs = _patch(monkeypatch, [FakeContainer("x", f"{host}:1")])
    n = sbx_probe.cleanup_orphans(same_machine=True, live_owners=set())
    assert n == 1
    assert cs[0].removed is True


def test_other_machines_never_touched(monkeypatch):
    """即使 owner 在存活集合里，跨机器的容器也不该被这台机器碰。"""
    host = owner_machine()
    cs = _patch(monkeypatch, [
        FakeContainer("far", "faraway:1"),
    ])
    # 存活集合里假装有它（不该生效）
    n = sbx_probe.cleanup_orphans(same_machine=True, live_owners={"faraway:1"})
    assert n == 0
    assert cs[0].removed is False


def test_all_owners_bypasses_live_check(monkeypatch):
    """运维手动清场（all_owners=True）必须无视存活状态。"""
    host = owner_machine()
    cs = _patch(monkeypatch, [
        FakeContainer("a", f"{host}:111"),
        FakeContainer("b", "other:1"),
    ])
    n = sbx_probe.cleanup_orphans(all_owners=True, live_owners={f"{host}:111"})
    assert n == 2
    assert all(c.removed for c in cs)


# ---------------------------------------------------------------- 实例登记

def test_live_owners_empty_without_redis(monkeypatch):
    """没配 Redis 时返回空集合，调用方退化成单实例语义。"""
    import asyncio
    monkeypatch.delenv("HLZ_REDIS_URL", raising=False)
    from app import cluster
    from app.cluster import instances
    cluster.reset_for_tests()
    assert asyncio.run(instances.live_owners()) == set()


def test_register_returns_false_without_redis(monkeypatch):
    import asyncio
    monkeypatch.delenv("HLZ_REDIS_URL", raising=False)
    from app import cluster
    from app.cluster import instances
    cluster.reset_for_tests()
    assert asyncio.run(instances.register_self()) is False