"""测试期回收守卫：把池里的容器销毁掉，且**绝不许挂死**。

## 为什么单独一个模块

`conftest` 的 teardown 需要「尽可能销毁 + 一定不卡住」这对矛盾语义。
直接 `asyncio.run(pool.shutdown())` 有两个坑：

1. **挂死**：`shutdown()` 里是 `async with pool._lock`。若锁在上一个
   事件循环里被某个中途退出的协程持有（`acquire()` 之后没 `release`），
   新循环的 `acquire()` 会**永久等待**。实测（`work/_probe_lock4.py`）：
   `locked=True` 时 `wait_for(3s)` 直接 TIMEOUT，裸 await 就是卡死。
   整个 pytest 会话会挂住 —— 比泄漏容器严重得多。

2. **异常吞掉后看不出问题**：`except Exception: pass` 会让「销毁一直失败」
   和「销毁成功」读数相同。所以这里返回值 + 日志都要有。

所以本模块只做两件事：带超时地拿锁销毁、把所有异常变成
「返回 0 + 打日志」，让调用方永远能继续往下走。
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .pool import ContainerPool

logger = logging.getLogger(__name__)

# 默认超时。销毁容器是 `to_thread(docker rm)`，2 个容器约几十毫秒；
# 60s 是给「Docker daemon 卡住」留的余量，不是给锁竞争用的。
DEFAULT_TIMEOUT_S = 30.0


async def release_pool(pool: "ContainerPool | None", *,
                       timeout_s: float = DEFAULT_TIMEOUT_S) -> int:
    """销毁池中全部容器，返回销毁数量。**保证在 timeout_s 内返回。**

    ## 语义

    - 正常：返回 `pool.shutdown()` 的销毁数；
    - 锁被占 / Docker 卡住：超时后返回 0 并打 WARNING，**不抛异常**；
    - 池是 None：返回 0。

    ## 为什么不在这里 `except Exception: pass`

    调用方（conftest teardown）确实要「清理失败不阻断测试」，但那属于
    调用方的策略。本函数负责**如实返回**，这样「一直销毁失败」与
    「从没有容器」在读数上不同（前者有 WARNING 日志、且池不变空）。
    """
    if pool is None:
        return 0
    try:
        n = await asyncio.wait_for(pool.shutdown(), timeout=timeout_s)
        if n:
            logger.info("测试 teardown 销毁了 %d 个沙箱容器", n)
        return int(n or 0)
    except asyncio.TimeoutError:
        # 最可能是锁被一个已经死掉的协程持有（见模块 docstring 坑 1）。
        # 放弃销毁而不是挂死：容器留着是垃圾，挂死是整个会话跑不完。
        left = _safe_size(pool)
        logger.warning(
            "测试 teardown 回收超时（%.0fs）：锁可能被已退出的协程持有；"
            "放弃销毁，池内仍登记 %s 个容器 —— 这些会在 Docker 里残留",
            timeout_s, left)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("测试 teardown 回收失败：%s: %s", type(exc).__name__, exc)
        return 0


def _safe_size(pool: object) -> str:
    """读池大小，读不到就返回 '?'（诊断用，绝不能因此抛异常）。"""
    try:
        return str(pool.size())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return "?"


def release_pool_sync(pool: "ContainerPool | None", *,
                      timeout_s: float = DEFAULT_TIMEOUT_S) -> int:
    """同步包装，供 `conftest` 这种非 async 的 teardown 调用。

    自己开一个事件循环 —— 因此**不能**在已有运行中的循环里调用
    （pytest 的同步 fixture 没有这个问题）。
    """
    try:
        return asyncio.run(release_pool(pool, timeout_s=timeout_s))
    except RuntimeError as exc:
        # "asyncio.run() cannot be called from a running event loop"
        logger.warning("测试 teardown 无法开事件循环：%s", exc)
        return 0
