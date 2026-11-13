"""P3 首帧提前 + 压缩互斥锁：单测。

验收目标（docs/20261112-Hlaudz首帧串行阻塞核实与优化设计.md §3.1/§3.2）：
  1. 压缩互斥锁：同一会话的软/硬线压缩**不并发**（防止丢失更新）；
  2. 不同会话互不阻塞（锁粒度是 (user, session)，不是全局）；
  3. 锁在异常后必须释放（否则该会话永久卡死）；
  4. `manual_compact` 与自动压缩共用同一把锁；
  5. 首帧顺序：`message_start` 在压缩之前发出（源码级断言，见 test_chat_prelude_order）。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.compaction import compaction_lock  # noqa: E402


# --------------------------------------------------------------------------
# 锁语义
# --------------------------------------------------------------------------

def test_same_session_lock_is_the_same_object():
    """同一 (user, session) 必须拿到同一把锁，否则互斥无效。"""
    a = compaction_lock("u1", "s1")
    b = compaction_lock("u1", "s1")
    assert a is b


def test_different_sessions_get_different_locks():
    """锁粒度是会话级：不同会话不该互相阻塞。"""
    a = compaction_lock("u1", "s1")
    b = compaction_lock("u1", "s2")
    c = compaction_lock("u2", "s1")
    assert a is not b
    assert a is not c


def test_lock_serializes_concurrent_writers():
    """核心：两个「读-改-写」序列不会交错。

    模拟丢失更新的经典场景：没有锁时两个协程都会在对方写入前读到旧值，
    后写的那个把前一个的结果覆盖掉。
    """
    async def main():
        state = {"steps": []}

        async def writer(name: str, delay: float):
            async with compaction_lock("u", "s"):
                state["steps"].append(f"{name}:read")
                await asyncio.sleep(delay)
                state["steps"].append(f"{name}:write")

        await asyncio.gather(writer("a", 0.03), writer("b", 0.01))
        return state["steps"]

    steps = asyncio.run(main())
    # 必须是一个完整的 read-write 对，再加另一个完整的对
    assert steps in (
        ["a:read", "a:write", "b:read", "b:write"],
        ["b:read", "b:write", "a:read", "a:write"],
    ), f"压缩序列交错，丢失更新风险：{steps}"


def test_lock_released_on_exception():
    """异常后锁必须释放，否则该会话之后永久卡死。"""
    async def main():
        try:
            async with compaction_lock("u", "s"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        # 若锁没释放，这里会永久挂起 -> 外层 wait_for 触发 TimeoutError
        async with compaction_lock("u", "s"):
            return True

    assert asyncio.run(asyncio.wait_for(main(), timeout=2.0)) is True


def test_different_sessions_do_not_block_each_other():
    """不同会话的压缩可以并行（否则多用户会互相拖慢）。"""
    async def main():
        order: list[str] = []

        async def slow():
            async with compaction_lock("u", "s1"):
                await asyncio.sleep(0.05)
                order.append("slow")

        async def fast():
            async with compaction_lock("u", "s2"):
                order.append("fast")

        await asyncio.gather(slow(), fast())
        return order

    order = asyncio.run(main())
    # fast 用的是另一把锁，不该等 slow
    assert order == ["fast", "slow"]


# --------------------------------------------------------------------------
# 三个压缩入口都必须持锁（源码级结构断言）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("func_name", [
    "manual_compact", "enforce_soft_limit", "enforce_hard_limit",
])
def test_all_compaction_entries_take_the_lock(func_name):
    """三个入口都要 `async with compaction_lock(...)`。

    为什么用源码断言：这类「忘了加锁」的缺陷在功能上完全看不出来
    （只在并发时丢数据），单测跑一次永远发现不了。

    为什么查 `async with` 而不是查 "compaction_lock" 字样：
    文档字符串里也会出现这个词（解释为什么需要锁），第一次写这条断言时
    就因为匹配到注释里的词而**变异测试没变红**——断言太松等于没断言。
    """
    import inspect
    import re

    from app import compaction

    fn = getattr(compaction, func_name)
    src = inspect.getsource(fn)
    # 只看真实语句，不看字符串/注释
    assert re.search(r"async\s+with\s+compaction_lock\s*\(", src), (
        f"{func_name} 没有真正 `async with compaction_lock(...)` 持锁"
    )


# --------------------------------------------------------------------------
# 首帧顺序（P3 的核心改动）
# --------------------------------------------------------------------------

def test_chat_prelude_order_message_start_before_compaction():
    """源码级断言：`message_start` 必须在压缩调用之前。

    这是 P3 的全部意义所在。真机验证见
    `tools/probe_soft_compact_blocking.py`（改造前超软线首帧 5859ms，
    改造后 531ms）。

    用源码断言而不是运行期断言，是因为这条顺序一旦被"顺手"改回去，
    功能上毫无变化，只有超软线会话的用户会感到「发送后卡 5 秒」——
    而单测里 MCP 都是假的、历史都很短，跑不出这个场景。
    """
    import inspect

    from app.routers import chat

    src = inspect.getsource(chat._event_stream)
    pos_start = src.find('yield frame("message_start"')
    pos_soft = src.find("enforce_soft_limit")
    pos_hard = src.find("enforce_hard_limit")

    assert pos_start >= 0, "找不到 message_start 的发出点"
    assert pos_soft >= 0, "找不到 enforce_soft_limit 调用"
    assert pos_hard >= 0, "找不到 enforce_hard_limit 调用"
    assert pos_start < pos_soft, (
        "message_start 在软线压缩之后 —— 超软线会话的首帧会被 LLM 摘要阻塞数秒"
    )
    assert pos_start < pos_hard, "message_start 在硬线裁剪之后"


def test_frame_helper_defined_before_first_use():
    """`frame()` 必须在 `message_start` 之前定义（提前首帧时最容易漏的引用错误）。"""
    import inspect

    from app.routers import chat

    src = inspect.getsource(chat._event_stream)
    pos_def = src.find("def frame(")
    pos_use = src.find('yield frame("message_start"')
    assert 0 <= pos_def < pos_use, "frame() 定义在首次使用之后会 NameError"