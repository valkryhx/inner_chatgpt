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
    """本实例的唯一标识（owner 的尾部，**不含机器名前缀**）。

    有 `HLZ_INSTANCE_ID` 时是它（净化后的）；没有时退化成本进程 PID。

    ⚠️ 2026-11-19 之前这个函数是**死代码** —— 全仓没人调用，
    所以 `HLZ_INSTANCE_ID` 从未生效过。现在真正的消费点是
    `sandbox.identity.owner_id()`（它直接读环境变量，不在启动期缓存，
    这样测试改环境变量立即生效）。这个函数保留下来是给**需要显示/上报**
    实例标识的地方用（日志、`/api/health` 扩展），值必须与 owner 的尾部一致。
    """
    from ..sandbox.identity import _instance_tag
    return _instance_tag()


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