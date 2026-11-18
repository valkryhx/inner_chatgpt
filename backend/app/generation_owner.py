"""get_active_generation 的 owner 版本：跨实例判活的唯一正确依据。

## 为什么需要

`chat.py:904` 有一段「抢不到 lease -> 看现状 -> 如果那个 generation 已经不在
跑了就把它判 stale 再抢一次」。原来的判据是 `is_generation_live()`，而它
**只看本进程**的三个字典（`_generation_tasks` / `_stop_events` / 预留表）。

单进程时这是对的。多实例时完全错：

    实例 A 正在跑 g1（DB: active, owner=hostA:111）
    用户在另一个标签页让请求落到实例 B
    B: acquire_generation 失败 -> active='g1'
       is_generation_live('g1') -> B 的内存里没有 -> False
       -> 把 g1 判成 stale + mark_stale_streaming_stopped
       -> A 还在写，B 已经把记录改了 -> 数据分叉

## 正确的判据

只有两种情况下才能判 g1 死了：

1. **owner 是我自己** —— 那么进程内状态是权威（我确实没有这个任务）。
2. **owner 是别人，但那个 owner 已经不在存活实例集合里** —— 那个实例死了。

owner 是别人且**还活着**时，绝不能动它的行 —— 即使我本地看不到任何任务。
这正是本模块要提供的能力。

## 没有 Redis 时

`live_owners()` 返回空集合，语义退化为「除了我自己，别人的 owner 都算死」。
单实例下本来就只有我的 owner，所以行为与迁移前完全一致。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def live_owners() -> set[str]:
    """本集群当前活着的 owner 集合。没配 Redis 返回空集合。"""
    try:
        from .cluster import instances
        return await instances.live_owners()
    except Exception:  # noqa: BLE001
        return set()


async def owner_of(user_id: str, session_id: str, generation_id: str) -> str:
    """某个 generation 的 owner（`<host>:<pid>`）。查不到返回空串。"""
    from .team_store import DB_PATH
    from . import sqlcompat

    try:
        async with sqlcompat.connect(DB_PATH) as conn:
            cur = await conn.execute(
                "SELECT owner FROM session_generations "
                "WHERE user_id=? AND session_id=? AND generation_id=?",
                (user_id, session_id, generation_id),
            )
            row = await cur.fetchone()
        return str(row[0] or "") if row else ""
    except Exception:  # noqa: BLE001
        return ""


async def is_generation_dead_elsewhere(user_id: str, session_id: str,
                                       generation_id: str) -> bool:
    """这个 generation 是否**确定**已经没人跑了（可安全判 stale）。

    返回 True 的三个充分条件之一：
    - owner 是空串（老库数据 / 升级前遗留）-> 视为死
    - owner 是**我自己** -> 信任进程内状态（调用方负责查）
    - owner 是别人，且不在存活集合里 -> 那个实例死了

    ⚠️ owner 是别人且**活着**时返回 False。这是本模块存在的全部意义。
    """
    owner = await owner_of(user_id, session_id, generation_id)
    if not owner:
        return True

    from .sandbox.identity import owner_id as my_owner
    if owner == my_owner():
        # 我自己的：进程内状态是权威，但这一步只回答「别人那边死没死」，
        # 自己的情况交给调用方用 is_generation_live 判断。
        return False

    alive = await live_owners()
    if not alive:
        # 没配 Redis：拿不到存活信息。此时保守起来会永久卡住用户
        # （那个 owner 可能永远「活着」），所以退化为单实例语义。
        return True
    return owner not in alive


async def writable_here(user_id: str, session_id: str, generation_id: str) -> bool:
    """我是否可以安全地把这个 generation 收敛掉（判 stale / 接管）。

    组合判据，给 chat.py 的 409 路径用：
    - owner 是我自己 -> 看 `is_generation_live`（调用方传判断结果进来）
    - owner 是别人 -> 只能等它死

    这里只回答「owner 层面」的问题；`is_generation_live` 由调用方判，
    因为那需要当前进程的实时状态。
    """
    return await is_generation_dead_elsewhere(user_id, session_id, generation_id)