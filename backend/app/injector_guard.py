"""给 `message_injector` 加超时预算，且**不丢消息**。

为什么需要这个模块
------------------
`agent_loop` 每轮模型调用前都跑一次注入器（团队收件箱 / 子代理通知 / 用户插话）。
这个调用原本是**裸 await、无超时**：注入器慢多久，整轮就等多久；注入器卡死，
整轮永不结束（见 docs/20261112-Hlaudz首帧串行阻塞核实与优化设计.md §1.3）。

为什么不能直接套 `asyncio.wait_for`
-----------------------------------
`wait_for` 超时会 **cancel** 内部协程。而 `team_store.consume_inbox` 的语义是
「取出的同时标记 consumed」（`team_store.py:750` 的 docstring 明确写了要让投递
at-most-once），它 `await conn.commit()` 之后才 `return rows`。于是：

    UPDATE ... state='consumed'   ← 已提交，消息视为已投递
    (cancel 打在这里)
    return rows                   ← 永远不会执行 -> 消息既没注入，也不会再被取到

结果是**静默丢消息**——比"卡住"更难排查，因为日志里什么都没有。

本模块的做法
------------
1. **超时不 cancel**：用 `asyncio.shield` 包住，超时只是「本轮不等了」；
2. **任务继续在后台跑完**，结果由**下一轮** `collect()` 取走；
3. **上一轮任务还没完成时不叠加新调用**：否则两次 `consume_inbox` 并发，
   会把同一批消息拆到两条注入路径上，顺序也会乱。

代价是「消息最多晚一轮到达」——优于「丢失」或「整轮卡死」。

残余风险（已知、已量化）
------------------------
若注入器超时后、下一轮 `collect()` 之前**本 generation 就结束了**，那条后台
task 的结果不会有消费者。`consume_inbox` 已经把消息标成 consumed，所以那批
消息仍会丢。

缓解：`AgentLoop.run_stream` 结束时调用 `flush()`，给后台 task 一个很短的
宽限（默认 2s）去完成并把结果写进日志。真丢的时候日志里有明确记录，
不会沉默。这个窗口很窄（要求超时恰好发生在最后一轮），且 8s 超时已远大于
DB 的 5s busy_timeout，正常情况下不会命中。

超时默认值
----------
默认 **8 秒**，而不是 3 秒。因为所有 DB 连接的 `PRAGMA busy_timeout` 都是 5000ms
（`team_store.py` / `history_store.py`），注入器最多会合法地等 5 秒锁。
把超时设在 5 秒以下，等于把「正常的锁等待」误判成「卡死」。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 默认 8s：必须大于 DB 的 busy_timeout=5000ms，否则正常锁等待会被当成卡死。
DEFAULT_INJECTOR_TIMEOUT_S = 8.0

# 收尾宽限：generation 结束时给未完成的后台注入任务多久去交结果。
FLUSH_GRACE_S = 2.0


def injector_timeout_from_env(default: float = DEFAULT_INJECTOR_TIMEOUT_S) -> float:
    """读 `HLZ_INJECTOR_TIMEOUT_S`。0 或负数 = 不设超时（退回历史行为）。"""
    raw = (os.environ.get("HLZ_INJECTOR_TIMEOUT_S") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else 0.0


class InjectorGuard:
    """包装一个注入器，加上超时预算并保证不丢消息。

    用法（`AgentLoop` 内部）：

        self._guard = InjectorGuard(injector, timeout_s)
        injected, timed_out = await self._guard.collect()
        ...
        await self._guard.flush()      # 本轮 generation 收尾时

    ``timed_out`` 只用于日志/可观测，**不改变本轮是否继续**：
    注入是「有更好、没有也能跑」的增强，绝不能因为它失败就中断这一轮。
    """

    __slots__ = ("_fn", "_timeout", "_task", "_timeout_hits", "_losers")

    def __init__(self, fn: Callable[[], Any], timeout_s: float) -> None:
        self._fn = fn
        self._timeout = float(timeout_s or 0.0)
        self._task: asyncio.Task | None = None
        self._timeout_hits = 0
        # 最终没能交付出去的消息（generation 结束时仍在飞的 task）。
        # 只用于日志与测试断言——真丢了必须有痕迹。
        self._losers: list[dict] = []

    # ---------- 主入口 ----------

    async def collect(self) -> tuple[list[dict], bool]:
        """取本轮要注入的消息。返回 ``(messages, timed_out)``。"""
        out: list[dict] = []

        # 1) 先收上一轮迟到的结果。
        #    有结果就直接用它作为本轮注入内容 —— **不再调用一次注入器**。
        #    理由：上一轮的注入器调用已经代表了「这一时刻该注入什么」，
        #    紧接着再调一次等于把同一批消息取两遍（consume_inbox 会把
        #    两条路径的消费拆开，顺序也会乱）。
        if self._task is not None:
            if self._task.done():
                out.extend(self._take_result())
                return out, False
            # 上一轮还没跑完：本轮不再叠加一次调用。
            return [], True

        # 2) 调用注入器
        try:
            raw = self._fn()
        except Exception:  # noqa: BLE001 - 注入器失败不能拖垮本轮
            logger.warning("注入器调用失败，本轮跳过注入", exc_info=True)
            return out, False

        # 同步注入器：没有超时问题，直接返回（省掉建 task 的开销）
        if not inspect.isawaitable(raw):
            out.extend(_normalize(raw))
            return out, False

        task = asyncio.ensure_future(raw)

        if self._timeout <= 0:
            # 显式关闭超时：保持历史行为（无限等）
            try:
                out.extend(_normalize(await task))
            except Exception:  # noqa: BLE001
                logger.warning("注入器执行失败，本轮跳过注入", exc_info=True)
            return out, False

        try:
            # shield：超时不会 cancel 内部 task，它会在后台跑完
            result = await asyncio.wait_for(asyncio.shield(task), timeout=self._timeout)
        except asyncio.TimeoutError:
            self._timeout_hits += 1
            self._task = task      # 留给下一轮
            logger.warning(
                "注入器超过 %.1fs 未返回，本轮先不注入（消息会在后续轮次补上）",
                self._timeout,
            )
            return out, True
        except Exception:  # noqa: BLE001
            logger.warning("注入器执行失败，本轮跳过注入", exc_info=True)
            task.cancel()
            return out, False
        else:
            out.extend(_normalize(result))
            return out, False

    # ---------- 收尾 ----------

    async def flush(self) -> list[dict]:
        """generation 结束时调用：给未完成的后台 task 一个短宽限。

        拿到结果就返回（调用方可以选择忽略——此时 generation 已结束，
        没有下一轮可注入）；拿不到就记一条**明确的**丢失日志。

        为什么返回而不是直接丢弃：调用方可能还有机会把它写进消息块或日志。
        """
        task = self._task
        if task is None:
            return []
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=FLUSH_GRACE_S)
            except asyncio.TimeoutError:
                self._task = None
                self._losers.append({"reason": "flush_timeout"})
                logger.error(
                    "注入器在 generation 结束时仍未返回（已等 %.0fs）。"
                    "该批消息可能已被 consume 但未注入，请检查团队收件箱是否有缺口。",
                    FLUSH_GRACE_S,
                )
                task.cancel()
                return []
            except Exception:  # noqa: BLE001
                logger.warning("收尾时注入器失败", exc_info=True)
                self._task = None
                return []
        # 到这里 task 已完成：交给 _take_result 统一取结果并清空 _task
        return self._take_result()

    # ---------- 内部 ----------

    def _take_result(self) -> list[dict]:
        """取走已完成 task 的结果（含异常处理），并释放 task。"""
        task, self._task = self._task, None
        if task is None:
            return []
        try:
            return _normalize(task.result())
        except asyncio.CancelledError:
            return []
        except Exception:  # noqa: BLE001
            logger.warning("后台注入器执行失败，其结果被丢弃", exc_info=True)
            return []

    # ---------- 可观测 ----------

    @property
    def timeout_hits(self) -> int:
        return self._timeout_hits

    @property
    def pending(self) -> bool:
        """是否还有在飞的后台注入任务（测试与收尾判断用）。"""
        return self._task is not None and not self._task.done()


def _normalize(raw: Any) -> list[dict]:
    """把注入器返回值收敛成 ``list[dict]``。

    容错是有意的：注入器来自不同模块（team / subagent / steer），
    任何一个返回 None 或脏数据都不该让整轮模型调用失败。
    """
    if not raw or not isinstance(raw, list):
        return []
    return [m for m in raw if isinstance(m, dict)]