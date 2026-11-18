"""容器归属：多进程 / 多机安全的命名与清扫。

## 要解决的问题

现在（单进程）没问题，但一旦扩容就会咬人：

1. **容器名固定** `hlz_sbx_<user_id>` —— 两个进程/两台机器同时建同名容器会冲突；
2. **清扫是全局的** —— 新进程启动时 `cleanup_orphans()` 会把**别的进程正在用的**
   容器一起删掉（现在只按 `hlz.sandbox=1` 过滤，没有归属信息）。

## 设计

给每个容器打两个归属标签：

    hlz.sandbox   = 1            # 本方案独占命名空间（不变）
    hlz.owner     = <owner_id>   # 谁创建的（机器 + 进程）

``owner_id`` 由「主机名 + 进程 PID」组成。为什么不只用 PID：多机上 PID 会撞
（都可能是 1234）。为什么不只用主机名：同机多进程会撞。

清扫规则：
- **默认只清自己的**（同 owner_id）—— 这是单机/多机都正确的行为
- 显式传 ``all_owners=True`` 才清全部（用于运维手动清场）

命名规则：
- 同机单进程时保持 `hlz_sbx_<user_id>`（可读性最好，也便于人工排查）
- 多进程/多机时带 owner 前缀，Docker 名称冲突从「报错」变成「天然隔离」

## 为什么不用 Redis 做这件事

容器只存在于**本机 Docker daemon** 里，天然有机器边界。owner 标签就能表达
「谁建的」，不需要跨机协调。Redis 是后面 `pool._entries` 外置时才需要的。
"""
from __future__ import annotations

import socket

import pytest


# ---------------- owner 标识 ----------------


def test_owner_id_contains_host_and_pid():
    """owner_id 必须是「主机名 + PID」，两者缺一都会撞。"""
    from app.sandbox.identity import owner_id
    oid = owner_id()
    assert socket.gethostname().split(".")[0] in oid or socket.gethostname() in oid, \
        f"owner_id 里没有主机名：{oid!r}"
    import os
    assert str(os.getpid()) in oid, f"owner_id 里没有 PID：{oid!r}"


def test_owner_id_is_filesystem_and_label_safe():
    """要能安全放进 Docker 标签值与容器名。"""
    from app.sandbox.identity import owner_id
    oid = owner_id()
    assert all(c.isalnum() or c in "-_.:" for c in oid), f"含非法字符：{oid!r}"
    assert len(oid) <= 128, "标签值过长"


# ---------------- 容器命名 ----------------


def test_container_name_is_stable_for_same_user_and_owner():
    """同一 owner + 同一用户 -> 名字恒定（复用查找依赖这个）。"""
    from app.sandbox.runtime import container_name
    a = container_name("u_x", owner="host1:100")
    b = container_name("u_x", owner="host1:100")
    assert a == b


def test_container_name_differs_across_owners():
    """不同 owner 的用户不能撞名 —— 这是多进程/多机的核心。"""
    from app.sandbox.runtime import container_name
    a = container_name("u_x", owner="host1:100")
    b = container_name("u_x", owner="host2:200")
    assert a != b, f"不同 owner 生成了同名容器：{a}"


def test_container_name_keeps_user_readable():
    """自包含 user_id，便于 `docker ps` 直接看出是谁的容器。"""
    from app.sandbox.runtime import container_name
    name = container_name("u_c56b8337dd09", owner="host1:100")
    assert "u_c56b8337dd09" in name
    assert name.startswith("hlz_sbx_")


# ---------------- 清扫只清自己的 ----------------


def test_cleanup_filters_by_owner_label(monkeypatch):
    """默认清扫必须带上 owner 过滤，不能只按 hlz.sandbox=1。

    这是本次改动的核心：不加 owner 过滤，新进程起来会删掉别人的容器。
    """
    from app.sandbox import probe as sbx_probe

    captured: dict = {}

    class FakeContainer:
        id = "c1"

        def remove(self, force=False):
            captured.setdefault("removed", []).append(self.id)

    class FakeContainers:
        @staticmethod
        def list(all=False, filters=None):
            captured["filters"] = filters
            return [FakeContainer()]

    class FakeClient:
        containers = FakeContainers()

        def close(self):
            pass

    monkeypatch.setattr(sbx_probe, "_client", lambda: FakeClient())
    sbx_probe.cleanup_orphans()

    filters = captured.get("filters") or {}
    assert isinstance(filters, dict)
    # 必须是 label 过滤，且包含 owner
    label = filters.get("label")
    assert label, f"没有按 label 过滤：{filters}"
    labels = label if isinstance(label, list) else [label]
    assert any("hlz.owner" in str(x) for x in labels), \
        f"清扫没有带 owner 过滤，会误删别人的容器：{labels}"


def test_cleanup_can_clear_all_owners_explicitly(monkeypatch):
    """运维手动清场时能清全部（显式开关，默认关闭）。"""
    from app.sandbox import probe as sbx_probe

    captured: dict = {}

    class FakeContainers:
        @staticmethod
        def list(all=False, filters=None):
            captured["filters"] = filters
            return []

    class FakeClient:
        containers = FakeContainers()

        def close(self):
            pass

    monkeypatch.setattr(sbx_probe, "_client", lambda: FakeClient())
    sbx_probe.cleanup_orphans(all_owners=True)

    label = (captured.get("filters") or {}).get("label")
    labels = label if isinstance(label, list) else [label]
    assert not any("hlz.owner" in str(x) for x in labels), \
        f"all_owners=True 时不该再带 owner 过滤：{labels}"


def test_container_creation_sets_owner_label():
    """创建容器时必须打上 owner 标签，否则清扫无从过滤。"""
    import inspect
    from app.sandbox.runtime import ContainerRuntime
    src = inspect.getsource(ContainerRuntime.create)
    assert "hlz.owner" in src or "OWNER_LABEL" in src, \
        "create() 没有设置 owner 标签"