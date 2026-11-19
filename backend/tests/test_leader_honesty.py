"""`/api/health` 的 leader 字段不能撒谎。

## 事故（真跑 compose --scale app=2 复现）

两个副本的 health 都报 `leader: true`。

根因：`_cluster_snapshot()` 读 `get_elector().is_leader`，而那个 elector
**从未 `start()` 过** —— 它的 `self._redis` 还是 `None`，
`is_leader` 属性直接短路返回 `True`（「没配 Redis 就是单实例」）。

真正用来决定「谁做启动清扫」的是 `sandbox.manager._is_cluster_leader()`：
它自己用一次性 `SET NX` 抢租约，与 elector 无关。

## 修法

两个选择：

1. 在 lifespan 里 `await get_elector().start()` —— 但 elector 目前
   **没有任何生产用途**（只有启动清扫用它，而清扫用的是独立租约）。
   启动一个只为 health 字段活着、还带后台循环的组件，是纯负担。
2. **health 不再报 leader**（改报「本实例是否持有启动清扫租约」这类
   真实信号），或者干脆去掉该字段。

选 2 的轻量版：保留字段但改成**如实反映** —— 只有 elector 真的启动过
（`is_running`）才报它的值，否则报 `false` 并说明「未启用 leader 选举」。
这样字段永不撒谎，且不强迫启动一个闲置组件。

## 反向确认

把 `is_leader` 改回无条件读 `get_elector().is_leader`，
`test_health_leader_reflects_elector_state` 必须变红。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def no_redis(monkeypatch):
    from app import cluster
    from app.cluster import leader
    monkeypatch.delenv("HLZ_REDIS_URL", raising=False)
    cluster.reset_for_tests()
    leader.reset_for_tests()
    yield
    cluster.reset_for_tests()
    leader.reset_for_tests()


def test_elector_reports_not_running_before_start(no_redis):
    """elector 在 `start()` 之前必须能自报「没在跑」。

    没有这个标志时，`is_leader` 会因为 `_redis is None` 短路返回 True，
    而调用方无法区分「单实例真 leader」和「多实例但选举没启动」。
    """
    from app.cluster.leader import get_elector
    e = get_elector()
    assert hasattr(e, "is_running"), "LeaderElector 没有 is_running 标志"
    assert e.is_running is False, "未启动的 elector 不该自称 running"


def test_is_leader_false_when_redis_configured_but_not_started(monkeypatch):
    """配了 Redis 但选举没启动 -> `is_leader` 必须是 False（不能撒谎）。"""
    from app import cluster
    from app.cluster import leader

    monkeypatch.setenv("HLZ_REDIS_URL", "redis://127.0.0.1:1/0")
    cluster.reset_for_tests()
    leader.reset_for_tests()
    try:
        e = leader.get_elector()
        assert e.is_leader is False, (
            "配了 Redis 但选举未启动，却自称 leader —— health 会报假信息")
    finally:
        cluster.reset_for_tests()
        leader.reset_for_tests()


def test_is_leader_true_without_redis(no_redis):
    """真·单实例（没配 Redis）仍然是 leader —— 这个语义不能丢。"""
    from app.cluster.leader import get_elector
    assert get_elector().is_leader is True


def test_health_leader_reflects_elector_state(monkeypatch):
    """**核心**：health 报的 leader 必须与 elector 真实状态一致。"""
    from fastapi.testclient import TestClient
    from app import cluster
    from app.cluster import leader
    from app.main import app as fastapi_app

    monkeypatch.setenv("HLZ_REDIS_URL", "redis://127.0.0.1:1/0")
    cluster.reset_for_tests()
    leader.reset_for_tests()
    try:
        with TestClient(fastapi_app) as c:
            body = c.get("/api/health").json()
        cluster_body = body["cluster"]
        assert cluster_body["leader"] is False, (
            f"选举没启动却报 leader=true：{cluster_body}")
        # 要能让运维看出「为什么不是 leader」
        assert cluster_body.get("leader_election") in ("off", "disabled", "on"), \
            f"缺少 leader 选举状态的说明：{cluster_body}"
        # 配了 Redis 且选举没跑 -> 必须是 disabled。
        # **不能是 "starting"**：那措辞让人以为「马上就好」，实际含义是
        # 「永远不会」—— LeaderElector 目前没有生产消费者（审计 §11.1）。
        assert cluster_body.get("leader_election") == "disabled", \
            f"配了 Redis 但选举未启用时应报 disabled，实际 {cluster_body.get('leader_election')!r}"
    finally:
        cluster.reset_for_tests()
        leader.reset_for_tests()