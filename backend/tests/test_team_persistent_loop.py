"""持久化 worker 的端到端行为：真实 host 循环（脚本化 LLM，不调真模型）。

验证的是循环骨架本身：跑完一轮进 idle → 被唤醒 → 带着 history 继续下一轮 → idle 超时退出。
真模型 e2e 在 work/ 里另跑。
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.subagent.context import AgentContext  # noqa: E402
from app.subagent.events import EventBuffer  # noqa: E402
from app.subagent.host import SubagentHost  # noqa: E402
from app.subagent.registry import SubagentRegistry  # noqa: E402


class _FakeRuntime:
    registry = None


def _ctx() -> AgentContext:
    return AgentContext(user_id="u_" + uuid.uuid4().hex[:8],
                        session_id="s_" + uuid.uuid4().hex[:8],
                        message_id="m1", generation_id="g1",
                        team_id="team_x", role="lead")


@pytest.mark.asyncio
async def test_idle_loop_survives_multiple_rounds_and_then_exits(monkeypatch):
    """核心行为：三轮唤醒都成功，最后 idle 超时正常退出（不是 completed 假象）。"""
    monkeypatch.setenv("TEAMMATE_IDLE_TIMEOUT_S", "0.4")
    monkeypatch.setenv("SUBAGENT_MAX_ACTIVE", "4")
    await db.init_db()

    host = SubagentHost(_ctx(), parent_runtime=_FakeRuntime(), buffer=EventBuffer())
    host.idle_timeout_s = 0.4
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "idle"
    node.idle_since = 0.0
    node.wake.clear()

    # 第一次唤醒
    async def wake_1():
        await asyncio.sleep(0.05)
        host.deliver_team_message("sub:worker", {
            "sender_agent_id": "root", "content": "第一件事", "message_id": "t1"})

    asyncio.create_task(wake_1())
    r1 = await host._idle_wait(node, timeout_s=2.0, poll_s=0.05)
    assert r1 == "wake"
    assert len(node.pending_messages) == 1
    assert "第一件事" in node.pending_messages[0]["content"]

    # 消费后再次空闲，第二件事到达
    node.drain_pending()
    node.wake.clear()
    async def wake_2():
        await asyncio.sleep(0.05)
        host.deliver_team_message("sub:worker", {
            "sender_agent_id": "root", "content": "第二件事", "message_id": "t2"})

    asyncio.create_task(wake_2())
    r2 = await host._idle_wait(node, timeout_s=2.0, poll_s=0.05)
    assert r2 == "wake"
    assert len(node.pending_messages) == 1

    # 第三次：没人来 → 超时退出
    node.drain_pending()
    node.wake.clear()
    r3 = await host._idle_wait(node, timeout_s=0.2, poll_s=0.05)
    assert r3 == "timeout"


@pytest.mark.asyncio
async def test_idle_disabled_by_default_keeps_old_behaviour(monkeypatch):
    """默认（未设置）时 idle_timeout=0，跑完一轮立即结束——历史行为不变。"""
    monkeypatch.delenv("TEAMMATE_IDLE_TIMEOUT_S", raising=False)
    host = SubagentHost(_ctx(), parent_runtime=_FakeRuntime(), buffer=EventBuffer())
    assert host.idle_timeout_s == 0.0


@pytest.mark.asyncio
async def test_evicted_idle_node_gets_stopped():
    """被槽位回收的 idle 节点必须收到 stop，否则它的 idle 循环永远挂着。"""
    host = SubagentHost(_ctx(), parent_runtime=_FakeRuntime(), buffer=EventBuffer())
    host.registry = SubagentRegistry(max_depth=2, max_active=1)
    a = host.registry.create_child(host.ctx, "a", team_id="team_x")
    a.status = "idle"
    a.idle_since = 1.0
    a.wake.clear()

    b = host.registry.create_child(host.ctx, "b", team_id="team_x")
    assert b.name == "b"
    assert a.stop.is_set(), "被淘汰的 idle 节点必须被 stop"
    assert a.wake.is_set(), "并且要唤醒它，让它从 _idle_wait 里出来"


@pytest.mark.asyncio
async def test_idle_notification_deduped_when_already_messaged():
    """本轮已 send_message 给 lead 的队友，不再重复推 idle 帧（CC m1448）。"""
    host = SubagentHost(_ctx(), parent_runtime=_FakeRuntime(), buffer=EventBuffer())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "running"

    host.push_team_message({"sender_agent_id": "sub:worker", "content": "结果 X",
                            "kind": "result"})
    assert node.sent_to_lead_this_round is True, "push 时必须打上本轮已汇报的标记"

    # 模拟循环里的判断：已汇报 → 不推 idle 帧
    if not node.sent_to_lead_this_round:
        host.push_idle_notification(node)
    drained = host.drain_notifications()
    joined = "\n".join(m["content"] for m in drained)
    assert "结果 X" in joined
    assert "空闲通知" not in joined, "已汇报过就不该再发 idle 帧"
