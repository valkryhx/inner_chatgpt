"""instance_registry：登记「本机有哪些活着的实例」。

## 解决的问题

启动清扫按机器名过滤，单实例时安全。但**同机跑两个后端进程**时：

```
t=0  实例 A 启动，建容器 hlz_sbx_u1_A     (owner = host:111)
t=5  实例 B 启动，清扫 same_machine
     -> 看到 hlz_sbx_u1_A 的 owner 是 host:111，以 host: 开头
     -> 判定为「本机残留」，删掉
     -> A 正在这个容器里跑用户代码，炸
```

「本机」这个粒度不够细，需要知道**哪些 owner 还活着**。

## 做法

每个实例启动时把 `owner_id()` 写进 Redis 的 `hlz:instances:<machine>` 有序集合
（score = 心跳时间戳 ms），定期刷新，退出时删除。

清扫时：本机上「owner 不在当前存活集合里」的容器才算残留。

## 一个键还是按机器分键（2026-11-19 真跑 compose 后改）

原实现用 `hlz:instances:<机器名>` 分键。单机多进程时没问题，但
**`--scale` 下每个副本的 hostname 不同**，于是两个实例永远在各自的键里：

```
app-1 (hostname d1ae4fbe797e) -> zadd hlz:instances:d1ae4fbe797e
app-2 (hostname 4c7af2741a10) -> zadd hlz:instances:4c7af2741a10
```

`live_owners()` 只返回自己那一个 -> `/api/health` 报 `live_instances=1`，
`is_generation_dead_elsewhere` 看不见远程 owner -> **判活整个失效**。

根因是一个函数承担了两种作用域：

| 调用方 | 真正需要的作用域 |
| --- | --- |
| `probe.cleanup_orphans`（清残留容器） | **本机**（容器只在本机 Docker 里） |
| `is_generation_dead_elsewhere`（判活） | **全集群** |
| `recover_stale_runtime`（收敛） | **全集群** |

所以改成**单一全局键** + 按 owner 的 `<机器名>:` 前缀过滤：
`live_owners()` 默认给全集群，`live_owners("hostA")` 给本机。
owner 里已经带了机器名，不需要再靠 key 编码这个维度。

## 没有 Redis 时

单实例，`live_owners()` 返回空集合，调用方退化成「按机器名前缀匹配」
（迁移前的行为），语义不变 —— 因为单实例下本机确实只有自己。

## owner 撞车的检测（2026-11-19 加）

登记表是「owner -> 心跳」的映射，**同一个 owner 只能有一个活着的实例**。
冲突的典型来源是 `HLZ_INSTANCE_ID` 没配好（或所有副本共用一个值）：

```
副本 A: owner = "web-0:1"   （容器里 PID 恒为 1）
副本 B: owner = "web-0:1"   <- 撞了
```

撞了之后，`live_owners()` 只返回一个成员，`is_generation_dead_elsewhere`
会在两个实例之间互相判对方「就是我自己」-> 跨实例判活整个失效，表现为
**随机数据分叉**。这种事故的排查成本极高（没有报错，只有数据对不上），
所以必须在登记时就**吼出来**。

做法：登记时顺带写一个带随机 token 的认领键（`SET NX`）。
  - 键不存在 -> 我认领，正常
  - 键已是**我自己的 token** -> 幂等重复登记（启动 + 心跳），正常
  - 键是**别人的 token** -> 有另一个进程在用同一个 owner，告警

**告警后仍然启动**：运维填错标识时拒绝启动会把「一个配置错误」升级成
「全站不可用」，而真正要防的伤害是静默，不是启动成功。
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Optional

from .client import get_redis
from .config import cluster_enabled
from ..sandbox.identity import owner_id, owner_machine, _instance_tag

logger = logging.getLogger(__name__)

# 心跳超过这个时长就算死了（实例被 kill -9 时不会走退出清理）
_STALE_MS = 90_000

# 本进程的认领 token：稳定（同一进程重复登记要能被认成「我自己」）
# 且跨进程不同（uuid4）。测试可以 monkeypatch 它来模拟另一个进程。
_TOKEN = uuid.uuid4().hex

# 冲突告警节流（按 owner 分别计时）。心跳每 30s 复核一次，冲突会持续存在；
# 不节流会把日志刷满，反而埋掉其他线索。
_CONFLICT_WARN_EVERY_MS = 300_000      # 5 分钟
_conflict_warned_at: dict[str, int] = {}


def _warn_conflict(owner: str) -> None:
    now_ms = int(time.time() * 1000)
    if now_ms - _conflict_warned_at.get(owner, 0) < _CONFLICT_WARN_EVERY_MS:
        return
    _conflict_warned_at[owner] = now_ms
    logger.warning(
        "实例标识冲突：owner=%s 已被另一个进程持有。"
        "同机的多个实例必须各自设置唯一的 HLZ_INSTANCE_ID"
        "（容器里 PID 恒为 1，靠 PID 区分不开）；"
        "否则跨实例判活与容器清扫的保护都会失效，表现为数据分叉。"
        "（同一 owner 每 %d 秒最多提示一次）",
        owner, _CONFLICT_WARN_EVERY_MS // 1000)


def reset_for_tests() -> None:
    """清掉告警节流状态（测试用：连续两个用例要各自看到一次告警）。"""
    _conflict_warned_at.clear()


def _key() -> str:
    """**全集群唯一**的心跳表键。

    ## 为什么不再按机器分键（2026-11-19）

    原实现是 `{prefix}:instances:{machine}`。单机多进程没问题，但
    `--scale` 下每个副本 hostname 不同 -> 各写各的键 -> `live_owners()`
    只看得到自己 -> 跨实例判活整个失效（详见模块头）。

    现在只有一个键，机器维度靠 owner 的 `<机器名>:` 前缀表达。
    需要「只看本机」的调用方传 `live_owners(machine=...)`。
    """
    prefix = os.environ.get("HLZ_REDIS_PREFIX", "hlz").strip() or "hlz"
    return f"{prefix}:instances"


def _claim_key(owner: str) -> str:
    """认领键。同样**不带机器前缀** —— 同 owner 跨机也要能发现冲突。"""
    return f"{_key()}:claim:{owner}"


async def _claim_owner(redis: Any, owner: str, *,
                       token: Optional[str] = None,
                       warn: bool = True) -> bool:
    """认领这个 owner，返回是否「没有冲突」。

    见模块头「owner 撞车的检测」。这里**不抛异常** —— 登记失败不该拖垮启动。
    """
    token = token or _TOKEN
    key = _claim_key(owner)
    try:
        got = await redis.get(key)
        if got is None:
            ok = await redis.set(key, token, nx=True, px=_STALE_MS * 2)
            if ok:
                return True
            # 竞态：并发的另一个进程先认领了，按「已存在」重判一次
            got = await redis.get(key)
        if got == token:
            # 幂等重复登记 / 心跳续期：顺手把认领键的 TTL 也续上，
            # 否则 2×_STALE_MS 后键会自然消失，同 owner 的另一个副本
            # 就能「悄悄认领成功」—— 检测只覆盖启动后的前 3 分钟。
            try:
                await redis.pexpire(key, _STALE_MS * 2)
            except Exception:  # noqa: BLE001
                pass
            return True
        if got and warn:
            # 另一个进程正在用同一个 owner -> 跨实例判活会失效
            _warn_conflict(owner)
        return not got
    except Exception:  # noqa: BLE001 - 检测失败按无冲突处理，不阻断启动
        logger.debug("owner 认领检测失败", exc_info=True)
        return True


async def register_self() -> bool:
    """登记本实例（幂等）。返回是否登记成功（没 Redis = False）。"""
    if not cluster_enabled():
        logger.info("instance_register skipped：未配置 Redis（单实例模式）")
        return False
    redis = await get_redis()
    if redis is None:
        logger.warning("instance_register skipped：Redis 客户端不可用")
        return False
    machine = owner_machine()
    me = owner_id()
    key = _key()
    now = int(time.time() * 1000)
    # 顺序：先认领再登记。认领只是检测，失败也照样登记 ——
    # 两个同 owner 的副本都应该出现在存活集合里，让上层「看起来」是对的，
    # 同时日志里已经有明确告警，运维能看到。
    await _claim_owner(redis, me)
    try:
        await redis.zadd(key, {me: now})
        await redis.pexpire(key, _STALE_MS * 2)
        logger.info("instance_register ok instance=%s machine=%s key=%s",
                    me, machine, key)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("instance_register failed instance=%s", me, exc_info=True)
        return False


async def heartbeat() -> None:
    """刷新本实例的心跳时间。由后台协程定期调用。"""
    if not cluster_enabled():
        return
    redis = await get_redis()
    if redis is None:
        return
    me = owner_id()
    key = _key()
    now = int(time.time() * 1000)
    try:
        await redis.zadd(key, {me: now})
        # 顺手清掉死掉的实例，避免集合无限增长
        await redis.zremrangebyscore(key, 0, now - _STALE_MS)
        await redis.pexpire(key, _STALE_MS * 2)
    except Exception:  # noqa: BLE001
        logger.debug("实例心跳失败", exc_info=True)
        return
    # 心跳顺便复核认领：`_claim_owner` 是 NX 的，启动时若因竞态漏判，
    # 或运维事后把两个副本改成同一标识，下一跳就能发现（30s 内）。
    await _claim_owner(redis, me)


async def unregister_self() -> None:
    """退出时把自己从登记表移除（正常 shutdown 走这条路）。"""
    if not cluster_enabled():
        return
    redis = await get_redis()
    if redis is None:
        return
    me = owner_id()
    try:
        await redis.zrem(_key(), me)
        logger.info("instance_unregister instance=%s", me)
    except Exception:  # noqa: BLE001
        logger.warning("instance_unregister failed instance=%s", me, exc_info=True)
    # 认领键也要放掉：否则同一 pod name 重启后会看到「上个进程的 token」，
    # 误报一次冲突（K8s 里 pod 重建后名字可能复用）。
    try:
        key = _claim_key(me)
        got = await redis.get(key)
        if got == _TOKEN:
            await redis.delete(key)
    except Exception:  # noqa: BLE001
        pass


async def live_owners(machine: Optional[str] = None) -> set[str]:
    """当前**活着**的 owner 集合。

    - `machine=None`（默认）：**全集群**。判活（`is_generation_dead_elsewhere`）、
      收敛（`recover_stale_runtime`）需要这个粒度。
    - 给了 `machine`：只返回该机器的（按 owner 的 `"<machine>:"` 前缀过滤）。
      容器清扫需要这个粒度 —— 容器只存在于本机 Docker 里。

    没配 Redis 返回空集合 —— 调用方据此退化成「按机器名前缀」的宽松模式。
    这正是单实例的正确语义（本机只有自己，所有本机容器都算残留）。
    """
    if not cluster_enabled():
        return set()
    redis = await get_redis()
    if redis is None:
        return set()
    now = int(time.time() * 1000)
    try:
        # 全集群一个键；机器维度靠 owner 前缀区分，不再靠 key。
        rows = await redis.zrangebyscore(_key(), now - _STALE_MS, "+inf")
        members = set(rows or [])
        if machine:
            prefix = f"{machine}:"
            members = {o for o in members if o.startswith(prefix)}
        # DEBUG 级别：这个函数被清理逻辑频繁调用，INFO 会刷屏。
        # 需要时把日志级别调到 DEBUG 就能看到完整的存活集合。
        logger.debug("live_owners machine=%s n=%d members=%s",
                     machine or "*", len(members), sorted(members))
        return members
    except Exception:  # noqa: BLE001
        logger.warning("live_owners failed machine=%s", machine, exc_info=True)
        return set()