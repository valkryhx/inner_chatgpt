"""实例标识：`HLZ_INSTANCE_ID` 必须真的参与 owner 推导。

## 遗留隐患（本次修）

`cluster.config.instance_id()` 曾是**死代码** —— 全仓从未调用过它。
后果：`HLZ_INSTANCE_ID` 完全没生效，owner 恒为 ``<hostname>:<pid>``。

容器里 PID 恒为 1，所以「所有副本 hostname 相同」的编排器（hostNetwork、
StatefulSet 未设 hostname、某些 PaaS）会让所有副本的 owner 都是 ``web-0:1``：

```
副本 A 在跑 g1（DB owner="web-0:1"）
副本 B 收到同一会话的请求
  B: is_generation_dead_elsewhere("g1")
       owner == my_owner -> 返回 False（"自己的，交给进程内判断"）
       B 的内存里没有 g1 -> False -> 判 stale -> 覆盖记录
  A 还在写 -> 数据分叉
```

一个 owner 撞了，`live_owners()` 也只剩 1 个成员，容器清扫的保护同时失效。

## 本次修的两件事

1. `identity.owner_id()` 真正使用 `HLZ_INSTANCE_ID`（保留 `<机器名>:` 前缀，
   否则 `probe.cleanup_orphans` 的 same_machine 前缀匹配会失效）；
2. 登记时检测「同一 owner 已有别人的心跳」，把冲突**吼出来** —— 不静默降级，
   因为这种部署错误的表现是数据分叉，不是启动失败。

## 反向确认

- 把 `identity._instance_tag()` 改回 `return str(os.getpid())`（忽略环境变量），
  `test_owner_differs_when_only_instance_id_differs` 必须变红；
- 把 `_claim_owner` 里的 `prev != tok` 判断去掉（无条件认领），
  `test_duplicate_instance_id_is_reported` 必须变红。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

COMPOSE = BACKEND / "deploy" / "docker-compose.yml"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fresh_conflict_throttle():
    """告警有 5 分钟节流；不重置的话连续两个用例只有一个能看到告警。"""
    from app.cluster import instances
    instances.reset_for_tests()
    yield
    instances.reset_for_tests()


# ---------------------------------------------------------------- owner 推导

def test_owner_falls_back_to_host_and_pid(monkeypatch):
    """未注入实例标识时，必须与迁移前**逐字一致**：``<hostname>:<pid>``。"""
    monkeypatch.delenv("HLZ_INSTANCE_ID", raising=False)
    from app.sandbox.identity import owner_id, owner_machine

    assert owner_id() == f"{owner_machine()}:{os.getpid()}"


def test_owner_uses_instance_id_when_set(monkeypatch):
    """注入后 owner 必须体现注入值 —— 这是「死代码被修好」的最小证据。"""
    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-a")
    from app.sandbox.identity import owner_id, owner_machine

    oid = owner_id()
    assert "pod-a" in oid, f"HLZ_INSTANCE_ID 没进 owner：{oid!r}"
    # 容器里 pid 恒为 1，不能再靠 pid 区分
    assert oid == f"{owner_machine()}:pod-a"


def test_owner_differs_when_only_instance_id_differs(monkeypatch):
    """**核心**：同机、同 PID（容器里都是 1）、仅实例标识不同 -> owner 必须不同。

    这就是用户指出的「所有副本 hostname 相同」场景：能不能区分全靠这个值。
    """
    from app.sandbox import identity

    monkeypatch.setattr(os, "getpid", lambda: 1)   # 模拟容器内 PID 1
    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-a")
    a = identity.owner_id()
    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-b")
    b = identity.owner_id()
    assert a != b, f"两个副本 owner 撞了：{a!r}"


def test_owner_keeps_machine_prefix(monkeypatch):
    """`<机器名>:` 前缀必须保留。

    `probe.cleanup_orphans(same_machine=True)` 用
    ``owner.startswith(f"{owner_machine()}:")`` 判断「本机容器」，
    前缀一变，启动清扫就再也不清本机残留了（静默泄漏）。
    """
    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-a")
    from app.sandbox.identity import owner_id, owner_machine

    assert owner_id().startswith(f"{owner_machine()}:")


def test_owner_is_label_and_name_safe_with_weird_instance_id(monkeypatch):
    """`HLZ_INSTANCE_ID` 是运维手填的，可能带 `/`、空格、大写。

    它要进 Docker 标签值和容器名，必须净化；净化后仍要能区分。
    """
    from app.sandbox import identity

    monkeypatch.setenv("HLZ_INSTANCE_ID", "ns/pod A 01")
    a = identity.owner_id()
    assert all(c.isalnum() or c in "-_.:" for c in a), f"含非法字符：{a!r}"
    assert len(a) <= 128, "标签值过长"

    monkeypatch.setenv("HLZ_INSTANCE_ID", "ns/pod B 01")
    b = identity.owner_id()
    assert a != b, "净化把两个不同标识抹平成了同一个"


def test_long_instance_id_stays_unique(monkeypatch):
    """超长标识必须截断**且不撞**（截断后还要留哈希尾巴）。

    容器名里 owner 被截到 64 字符（`runtime.container_name`），
    如果 owner 本身只是长前缀，两个副本会撞在同名容器上。
    """
    from app.sandbox import identity

    head = "pod-" + "x" * 80
    monkeypatch.setenv("HLZ_INSTANCE_ID", head + "-a")
    a = identity.owner_id()
    monkeypatch.setenv("HLZ_INSTANCE_ID", head + "-b")
    b = identity.owner_id()
    assert a != b, f"长标识截断后撞了：{a!r}"
    assert len(a) <= 72, f"owner 过长会撑爆容器名/标签：{len(a)}"


def test_container_name_differs_for_same_hostname_replicas(monkeypatch):
    """最终防线：容器名不能撞（撞了就是 `Conflict. name already in use`）。"""
    from app.sandbox import identity
    from app.sandbox.runtime import container_name

    monkeypatch.setattr(os, "getpid", lambda: 1)
    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-a")
    a = container_name("u1", owner=identity.owner_id())
    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-b")
    b = container_name("u1", owner=identity.owner_id())
    assert a != b, f"两个副本为同一用户算出同名容器：{a!r}"


# ---------------------------------------------------------------- 冲突检测

class FakeRedis:
    """只实现 `_claim_owner` 用到的几个命令（不需要真 Redis）。"""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.zsets: dict[str, dict[str, int]] = {}

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, *, px=None, nx=False):
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def delete(self, *keys):
        for k in keys:
            self.kv.pop(k, None)
        return len(keys)

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def pexpire(self, key, ms):
        return True

    async def zrem(self, key, member):
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0

    async def zremrangebyscore(self, key, lo, hi):
        return 0


def test_same_process_reclaim_is_not_a_conflict(caplog):
    """同一进程重复认领（幂等登记 / 心跳）不能刷警告。"""
    from app.cluster import instances

    fake = FakeRedis()
    with caplog.at_level(logging.WARNING, logger=instances.logger.name):
        run(instances._claim_owner(fake, "m1", "m1:pod-a", token="tok-a"))
        run(instances._claim_owner(fake, "m1", "m1:pod-a", token="tok-a"))
    assert not [r for r in caplog.records if "实例标识冲突" in r.getMessage()]


def test_duplicate_instance_id_is_reported(caplog):
    """**核心**：两个进程认领同一 owner -> 必须吼出来。

    否则表现是「跨实例判活随机失效 + 数据分叉」，比启动失败难查得多。
    """
    from app.cluster import instances

    fake = FakeRedis()
    with caplog.at_level(logging.WARNING, logger=instances.logger.name):
        first = run(instances._claim_owner(fake, "m1", "m1:pod-a", token="tok-a"))
        second = run(instances._claim_owner(fake, "m1", "m1:pod-a", token="tok-b"))

    assert first is True, "第一个认领不该被判成冲突"
    assert second is False, "第二个进程必须知道 owner 被别人占了"
    msgs = [r.getMessage() for r in caplog.records if "实例标识冲突" in r.getMessage()]
    assert msgs, "冲突没有告警"
    assert "HLZ_INSTANCE_ID" in msgs[0], f"告警没给出可操作的修法：{msgs[0]}"


def test_different_owners_do_not_conflict(caplog):
    """不同 owner 并发认领是正常情况（同机两个实例），不能告警。"""
    from app.cluster import instances

    fake = FakeRedis()
    with caplog.at_level(logging.WARNING, logger=instances.logger.name):
        a = run(instances._claim_owner(fake, "m1", "m1:pod-a", token="tok-a"))
        b = run(instances._claim_owner(fake, "m1", "m1:pod-b", token="tok-b"))
    assert (a, b) == (True, True)
    assert not [r for r in caplog.records if "实例标识冲突" in r.getMessage()]


def test_register_self_claims_owner(monkeypatch, caplog):
    """`register_self()` 必须走认领流程，且**冲突时仍然启动**。

    拒绝启动会让「运维填了重复标识」变成全站不可用；
    真正的伤害是静默分叉，所以：警告 + 继续。
    """
    from app.cluster import instances

    fake = FakeRedis()

    async def fake_get_redis():
        return fake

    monkeypatch.setenv("HLZ_REDIS_URL", "redis://127.0.0.1:0/0")
    monkeypatch.setenv("HLZ_REDIS_PREFIX", "hlzt")
    monkeypatch.setattr(instances, "get_redis", fake_get_redis)

    with caplog.at_level(logging.WARNING, logger=instances.logger.name):
        assert run(instances.register_self()) is True
        # 假装是另一个进程：换 token 再登记一次
        monkeypatch.setattr(instances, "_TOKEN", "other-token")
        assert run(instances.register_self()) is True, "冲突时不该拒绝启动"

    assert [r for r in caplog.records if "实例标识冲突" in r.getMessage()]


# ---------------------------------------------------------------- 编排配置

def test_compose_does_not_inject_host_hostname():
    """compose 里不能再把宿主的 `${HOSTNAME}` 插值给 `HLZ_INSTANCE_ID`。

    `docker compose` 的 `${HOSTNAME}` 是从**宿主 shell** 插值的，
    所有副本拿到同一个值 —— 一旦实例标识生效，这行就是「保证 owner 撞车」。

    只检查**生效的配置行**（跳过注释）：注释里当然会提到这个错误写法，
    否则这条断言会因为「有人写了正确的反例注释」而变红。
    """
    assert COMPOSE.exists(), f"找不到 {COMPOSE}"
    raw_lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    live = []
    for n, line in enumerate(raw_lines, 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        live.append((n, s))

    for n, s in live:
        if not s.startswith("HLZ_INSTANCE_ID:"):
            continue
        value = s.split(":", 1)[1].strip()
        assert "HOSTNAME" not in value, (
            f"docker-compose.yml:{n} 用宿主 HOSTNAME 插值 HLZ_INSTANCE_ID"
            f"（{s!r}），所有副本会拿到同一个值 -> owner 撞车")
        assert "${" in value or value == '""', (
            f"docker-compose.yml:{n} 给 HLZ_INSTANCE_ID 写了固定值"
            f"（{s!r}），所有副本会撞车")