"""把软线压缩延到本轮结束后执行（P4）。

为什么需要
----------
P3 之后首帧已经很快（超软线 6094ms → 531ms），但**首个 token 仍被软线压缩推迟**：
压缩只是从"首帧之前"挪到了"首帧之后、送模型之前"，用户仍然要等摘要跑完才看到字。

软线压缩的两个事实决定了它根本不必在关键路径上：

1. **它不影响本轮能否开始**。硬线（超窗）才是正确性问题；软线只是"提前摘要，
   省点 token"，摘要晚一轮做，对当前这轮的回答毫无影响。
2. **它的产物对下一轮同样有效**。`enforce_soft_limit` 的输入是「历史 + 本轮输入」，
   本轮结束后输入已进历史，所以赛后压缩的结果与赛前压缩等价。

所以做法是：**本轮检测到超软线时不压缩，直接放行；本轮结束后在后台补压缩**。

设计约束（每条都对应一个真实风险）
----------------------------------
- **不能阻塞 `done`**：用户看到"完成"后不该还等压缩。所以放在 `done` 之后，
  且用后台 task。
- **必须持压缩锁**：后台压缩与本轮/下一轮的硬线裁剪会撞，靠 `compaction_lock` 串行。
- **必须能被下一轮打断**：用户可能立刻发下一条。下一轮开始时若后台压缩还在跑，
  它的锁会让本轮硬线等待——所以后台压缩要设超时，别把下一轮拖住。
- **失败必须静默**：压缩失败只该让下一轮再压一次，绝不能让用户看到错误。
- **可关闭**：`HLZ_SOFT_COMPACT_DEFER=0` 退回历史行为（一律赛前压缩）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# 后台压缩的墙钟上限。超过就放弃（下一轮还会再试），避免拖住后续轮次。
DEFAULT_DEFERRED_TIMEOUT_S = 60.0

# 每个会话至多一个在飞的赛后压缩任务。
_pending: dict[tuple[str, str], asyncio.Task] = {}


def soft_defer_enabled() -> bool:
    """软线压缩是否延到本轮之后。默认开（P4 的目标行为）。

    关闭（`HLZ_SOFT_COMPACT_DEFER=0`）退回 P3 的行为：赛前压缩，
    首个 token 会等摘要。留这个开关是因为赛后压缩依赖"下一轮会再进这条路径"，
    如果某个部署是**一次性长会话**（发一次就不再发），赛后压缩就跑不到。
    """
    raw = (os.environ.get("HLZ_SOFT_COMPACT_DEFER") or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def deferred_timeout_from_env(default: float = DEFAULT_DEFERRED_TIMEOUT_S) -> float:
    raw = (os.environ.get("HLZ_SOFT_COMPACT_TIMEOUT_S") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else 0.0


def schedule_soft_compact(user_id: str, session_id: str) -> asyncio.Task | None:
    """在后台补跑一次软线压缩。返回 task（调用方通常不关心结果）。

    幂等：同一会话已有在飞任务时不重复启动——重复启动没有意义（第二次几乎
    必然读到刚压完的历史、直接返回 None），还会白占一把锁。
    """
    if not soft_defer_enabled():
        return None
    key = (str(user_id), str(session_id))
    cur = _pending.get(key)
    if cur is not None and not cur.done():
        return cur

    task = asyncio.ensure_future(_run(user_id, session_id, key))
    _pending[key] = task

    def _cleanup(_t: asyncio.Task) -> None:
        # 只清理自己，避免把后来者的任务删掉
        if _pending.get(key) is _t:
            _pending.pop(key, None)

    task.add_done_callback(_cleanup)
    return task


async def _run(user_id: str, session_id: str, key: tuple[str, str]) -> None:
    timeout = deferred_timeout_from_env()
    try:
        from .compaction import enforce_soft_limit
        if timeout > 0:
            await asyncio.wait_for(
                enforce_soft_limit(user_id, session_id), timeout=timeout)
        else:
            await enforce_soft_limit(user_id, session_id)
    except asyncio.TimeoutError:
        logger.warning("赛后压缩超过 %.0fs 未完成，放弃（下一轮会再试）", timeout)
    except asyncio.CancelledError:
        # 进程退出/会话关闭时被取消是正常的
        raise
    except Exception:  # noqa: BLE001 - 赛后压缩失败绝不能冒到用户面前
        logger.warning("赛后压缩失败（不影响本轮结果）", exc_info=True)


async def preempt(user_id: str, session_id: str, timeout_s: float = 2.0) -> bool:
    """取消该会话在飞的赛后压缩，让**新一轮**优先。

    为什么必须有这个：赛后压缩持有着 `compaction_lock`。用户如果立刻发下一条，
    那一轮开头的硬线裁剪要拿同一把锁 —— 若上一轮的摘要还在跑（最长 60s），
    新的一轮就被上一轮的后台任务拖住了。P3 好不容易把首帧降下来，
    不能因为 P4 又把它加回去。

    取消是安全的：`enforce_soft_limit` 幂等，被取消只是少做一次摘要，
    本轮结束后会重新调度。

    返回 True 表示确实抢占了一个在飞任务。
    """
    key = (str(user_id), str(session_id))
    task = _pending.get(key)
    if task is None or task.done():
        return False
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
        # 被取消 / 等不到 / 任务自己抛异常，都不影响调用方继续
        pass
    if _pending.get(key) is task:
        _pending.pop(key, None)
    return True


async def drain_all(timeout_s: float = 5.0) -> int:
    """等待所有在飞的赛后压缩结束（测试与优雅关闭用）。返回等待的任务数。"""
    tasks = [t for t in _pending.values() if not t.done()]
    if not tasks:
        return 0
    await asyncio.wait(tasks, timeout=timeout_s)
    return len(tasks)


def pending_count() -> int:
    return sum(1 for t in _pending.values() if not t.done())