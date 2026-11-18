"""多实例协调层：Redis 客户端 / 分布式锁 / leader election。

## 为什么需要这一层

单机单进程时，下面这些「进程内状态」是安全的；一旦跑两个实例就全变成 bug：

| 进程内状态 | 多实例下会发生什么 |
| --- | --- |
| 启动清扫沙箱容器 | 新实例把**另一个实例正在用**的容器删掉 |
| `recover_stale_runtime()` | 新实例把**另一个实例正在跑**的 generation 标成 stale |
| `runtime._stop_events` | 用户在实例 B 点停止，生成在实例 A 上，停不掉 |
| `steer_queue._queues` | 用户中途输入排在实例 A，重试落到实例 B，消息丢 |
| `pg_advisory_xact_lock(固定键)` | 所有队的写事务排成一队（已实测 3.8 倍差距） |

这一层提供三块地基：

1. `client`  —— 进程级 Redis 客户端（按 event loop 缓存，同 asyncpg 的坑）
2. `lock`    —— `SET NX PX` + Lua 释放的分布式锁，带续约
3. `leader`  —— 带租约的 leader election（防惊群）

## 铁律：**没有 Redis 必须照常工作**

`HLZ_REDIS_URL` 不设时：
- `lock()` 退化成进程内 `asyncio.Lock`（单实例下语义等价）
- `LeaderElector.is_leader` 恒为 `True`（单实例本来就是唯一实例）

这样本地开发和现有测试不需要 Redis，行为与迁移前完全一致。
"""
from __future__ import annotations

from .config import cluster_enabled, redis_url, reset_for_tests
from .client import get_redis, close_redis
from .lock import LockHandle, distributed_lock
from .leader import LeaderElector, get_elector

__all__ = [
    "cluster_enabled",
    "redis_url",
    "reset_for_tests",
    "get_redis",
    "close_redis",
    "LockHandle",
    "distributed_lock",
    "LeaderElector",
    "get_elector",
]