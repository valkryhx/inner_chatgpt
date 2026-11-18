"""多实例相关的配置读取。

集中在这里而不是散在 `config.py`，是因为 `config.py` 已经背了 LLM / 沙箱 /
工作区一堆职责，而多实例是**另一个维度**的开关（部署形态）。
独立模块也让测试能干净地 `reset_for_tests()` 而不影响别的配置。
"""
from __future__ import annotations

import os
from typing import Optional

_DISABLED = ("", "0", "false", "False", "off", "no")


def redis_url() -> Optional[str]:
    """Redis 连接串。空 = 单实例模式，所有协调能力走进程内降级。"""
    raw = (os.environ.get("HLZ_REDIS_URL") or "").strip()
    return raw or None


def cluster_enabled() -> bool:
    """是否启用多实例协调（= 有没有配 Redis）。"""
    return redis_url() is not None


def instance_id() -> str:
    """本实例的唯一标识。容器化部署时应由环境变量注入（如 K8s pod name）。"""
    raw = (os.environ.get("HLZ_INSTANCE_ID") or "").strip()
    if raw:
        return raw
    from ..sandbox.identity import owner_id
    return owner_id()


def is_leader() -> bool:
    """同步判断「我是不是唯一实例」。

    只用于**启动期一次性的**决策（要不要清扫残留沙箱）。
    长期判断请用 `cluster.get_elector().is_leader`。
    """
    raw = (os.environ.get("HLZ_IS_LEADER") or "").strip()
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    return not cluster_enabled()


def lock_ttl_ms() -> int:
    """分布式锁的默认租期（毫秒）。到期未续约则视为持有者已死。"""
    return _int_env("HLZ_LOCK_TTL_MS", 10000, lo=1000, hi=300000)


def lock_wait_ms() -> int:
    """等锁的默认上限（毫秒）。超时抛 `LockTimeout`。"""
    return _int_env("HLZ_LOCK_WAIT_MS", 5000, lo=0, hi=300000)


def leader_ttl_ms() -> int:
    """leader 租期（毫秒）。"""
    return _int_env("HLZ_LEADER_TTL_MS", 25000, lo=3000, hi=600000)


def leader_refresh_ms() -> int:
    """leader 续约间隔（毫秒）。必须显著小于租期。"""
    ttl = leader_ttl_ms()
    return _int_env("HLZ_LEADER_REFRESH_MS", max(ttl // 3, 1000), lo=500, hi=ttl)


def _int_env(name: str, default: int, *, lo: int, hi: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(int(raw), hi))
    except ValueError:
        return default


def reset_for_tests() -> None:
    """占位：本模块无缓存，测试改环境变量后立即生效。

    保留这个函数是为了与 `dbselect.reset_for_tests()` 的用法一致，
    让测试可以无脑调用而不必记住哪个模块有缓存。
    """