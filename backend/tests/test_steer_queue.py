"""用户中途插话队列（steering）的单元测试。

对应 docs/20261111-用户中途插话steering实施方案.md §4 的步 1、2。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.steer_queue import (  # noqa: E402
    MAX_QUEUE_DEPTH,
    MAX_STEER_CHARS,
    SteerQueue,
    drop_queue,
    get_queue,
    wrap_user_steer,
)


def test_push_and_drain_roundtrip():
    q = SteerQueue()
    assert q.push("先看看日志") == 1
    assert q.push("顺便统计行数") == 2

    items = q.drain()

    assert [i["text"] for i in items] == ["先看看日志", "顺便统计行数"]
    assert q.drain() == [], "drain 必须取走即清"


def test_drain_limit_takes_from_head():
    q = SteerQueue()
    for i in range(5):
        q.push(f"m{i}")

    first = q.drain(limit=2)

    assert [i["text"] for i in first] == ["m0", "m1"]
    assert [i["text"] for i in q.snapshot()] == ["m2", "m3", "m4"]


def test_snapshot_does_not_consume():
    q = SteerQueue()
    q.push("还在队列里")

    assert len(q.snapshot()) == 1
    assert len(q.snapshot()) == 1, "snapshot 不能消费"
    assert len(q.drain()) == 1


def test_empty_and_blank_are_rejected():
    q = SteerQueue()
    assert q.push("") == 0
    assert q.push("   ") == 0
    assert q.push(None) == 0
    assert q.snapshot() == []


def test_text_is_capped():
    """单条限长：插话是补一句要求，不是二次投喂整篇文档。"""
    q = SteerQueue()
    q.push("字" * (MAX_STEER_CHARS + 500))

    item = q.snapshot()[0]
    assert len(item["text"]) == MAX_STEER_CHARS


def test_depth_is_capped():
    """限深：防止客户端刷屏把上下文撑爆。"""
    q = SteerQueue()
    for i in range(MAX_QUEUE_DEPTH):
        assert q.push(f"m{i}") == i + 1

    depth = q.push("overflow")

    assert depth == 0, f"超深应被拒绝（返回 0），实际 {depth}"
    assert len(q.snapshot()) == MAX_QUEUE_DEPTH


def test_clear_returns_count():
    q = SteerQueue()
    q.push("a")
    q.push("b")

    assert q.clear() == 2
    assert q.snapshot() == []


def test_drain_is_atomic_under_concurrent_push():
    """GA 的坑 2：UI 线程 push、回合线程 drain，读-清两步会丢中间到达的消息。

    这里的 drain 用「整体替换列表」实现，所以并发 push 不会丢。
    """
    q = SteerQueue()

    async def scenario():
        stop = asyncio.Event()
        pushed = []

        async def pusher():
            for i in range(200):
                q.push(f"p{i}")
                pushed.append(f"p{i}")
                await asyncio.sleep(0)
            stop.set()

        drained: list[str] = []

        async def drainer():
            while not stop.is_set() or q.snapshot():
                drained.extend(i["text"] for i in q.drain(limit=3))
                await asyncio.sleep(0)

        await asyncio.gather(pusher(), drainer())
        drained.extend(i["text"] for i in q.drain())
        return pushed, drained

    pushed, drained = asyncio.run(scenario())

    assert sorted(drained) == sorted(pushed), (
        f"并发下丢了消息：推入 {len(pushed)}，取到 {len(drained)}")
    assert len(drained) == len(set(drained)), "不能有重复"


def test_identical_messages_both_delivered():
    """两条内容一样的消息是两次独立补充，不能被去重掉。"""
    q = SteerQueue()
    q.push("继续")
    q.push("继续")

    items = q.drain()

    assert len(items) == 2
    assert items[0]["id"] != items[1]["id"], "每条要有独立 id 供前端回显"


# ---------------- 注册表：跨回合存活 ----------------
def test_queue_survives_between_turns():
    """GA 的坑 1：回合结束后到达的消息必须能活到下一回合。

    这里只验证「注册表拿得到同一个队列对象」，回合生命周期的清理在
    runtime 侧（见 test_steer_runtime_registry）。
    """
    key = ("u1", "s1", "g1")
    q1 = get_queue(*key)
    q1.push("回合末尾才到的一句")

    q2 = get_queue(*key)

    assert q2 is q1
    assert [i["text"] for i in q2.drain()] == ["回合末尾才到的一句"]
    drop_queue(*key)


def test_get_queue_is_per_generation():
    a = get_queue("u1", "s1", "g1")
    b = get_queue("u1", "s1", "g2")

    assert a is not b
    a.push("给 g1")
    assert b.snapshot() == []

    drop_queue("u1", "s1", "g1")
    drop_queue("u1", "s1", "g2")


# ---------------- 包装文案 ----------------
def test_wrapper_makes_it_a_supplement_not_a_new_question():
    """GA 的坑 3：注入文本必须能区分，否则模型会重新回答原来那个问题。"""
    wrapped = wrap_user_steer("再补一句西湖的内容")

    assert "再补一句西湖的内容" in wrapped
    assert "执行过程中" in wrapped
    assert "不要当成一个新的对话回合" in wrapped
    assert "不要重新回答" in wrapped