"""P-A 持久 worker 的边界摸排：并发、停止、唤醒风暴、SSE 关闭时机。

这些是"实现能跑"之后真正会出问题的地方。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
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


def _host() -> SubagentHost:
    return SubagentHost(_ctx(), parent_runtime=_FakeRuntime(), buffer=EventBuffer())


# ---------- 边界 1：多个 idle 队友同时被唤醒，不能互相吞掉消息 ----------
@pytest.mark.asyncio
async def test_multiple_idle_agents_wake_independently():
    host = _host()
    a = host.registry.create_child(host.ctx, "a", team_id="team_x")
    b = host.registry.create_child(host.ctx, "b", team_id="team_x")
    for n in (a, b):
        n.status = "idle"
        n.wake.clear()

    host.deliver_team_message("sub:a", {"content": "给 a", "message_id": "t1"})
    assert a.wake.is_set() and not b.wake.is_set(), "只该唤醒被投递的那个"
    assert len(a.pending_messages) == 1 and not b.pending_messages

    host.deliver_team_message("sub:b", {"content": "给 b", "message_id": "t2"})
    assert b.wake.is_set()
    assert "给 b" in b.pending_messages[0]["content"]
    assert "给 a" not in b.pending_messages[0]["content"], "不能串消息"


# ---------- 边界 5：实时投递过的消息，下一轮补投不能重复注入 ----------
@pytest.mark.asyncio
async def test_live_delivered_message_is_not_refilled_next_round():
    """真实缺陷：lead 给 idle 队友发消息时走实时投递，库里标成 delivered 就停住了。
    队友下一轮 _refill_inbox 用 consume_inbox 把 state IN ('queued','delivered')
    全取出来，于是同一件事被安排两遍。真机 steer 场景里 state 停在 delivered
    就是这个缺陷的痕迹。
    """
    from app import db, team_store
    from app.scope import TeamScope
    from app.team_host import TeamHost

    await db.init_db()
    await team_store.init_tables()

    host = _host()
    uid, sid = host.ctx.user_id, host.ctx.session_id
    node = host.registry.create_child(host.ctx, "writer", team_id="team_x")
    node.status = "idle"
    node.wake.clear()

    # 子代理视角的收件箱（与 SubagentRuntime 里构造的一致）
    child_scope = TeamScope(user_id=uid, session_id=sid, generation_id="g1",
                            team_id="team_x", agent_id="sub:writer", role="teammate")
    child_host = TeamHost(child_scope, deliver_sink=host.deliver_team_message)

    row = await team_store.send_message(
        child_scope, recipient="sub:writer", kind="request", content="再补一句西湖")

    # 真实投递路径：推给活体 → 打 delivered 标记
    out = host.deliver_team_message("sub:writer", row)
    assert out["delivered"] is True
    await team_store.mark_messages_delivered(child_scope, [row["message_id"]])
    assert len(node.pending_messages) == 1
    node.drain_pending()  # 这一轮已经把它注入模型了

    # 下一轮开始前补投：这条已经交付过的消息不能再进来
    runtime = type("_R", (), {"team_host": child_host})()
    await host._refill_inbox(node, runtime)

    assert len(node.pending_messages) == 0, (
        "同一消息被重复注入："
        f"{[m['content'][:30] for m in node.pending_messages]}")

    await team_store.delete_session_data(uid, sid)


# ---------- 边界 5b：lead 收件箱同样不能重复注入 ----------
@pytest.mark.asyncio
async def test_live_delivered_message_is_not_reinjected_into_lead_inbox():
    """同一条缺陷在 lead 侧的表现：实时推过的消息，下一轮 turn 开始的
    deliver_pending_inbox 不能再取一次。"""
    from app import db, team_store
    from app.scope import TeamScope
    from app.team_host import TeamHost

    await db.init_db()
    await team_store.init_tables()

    host = _host()
    uid, sid = host.ctx.user_id, host.ctx.session_id
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "running"

    lead_scope = TeamScope(user_id=uid, session_id=sid, generation_id="g1",
                           team_id="team_x", agent_id="root", role="lead")
    lead_host = TeamHost(lead_scope, deliver_sink=host.push_team_message)

    row = await team_store.send_message(
        lead_scope, recipient="root", kind="result", content="第一轮结论 4242")
    host.push_team_message(row)
    await team_store.mark_messages_delivered(lead_scope, [row["message_id"]])
    host.drain_notifications()

    n = await host.deliver_pending_inbox(lead_host)

    assert n == 0, f"已交付过的消息被再次注入 {n} 条"
    assert host.drain_notifications() == []
    await team_store.delete_session_data(uid, sid)


# ---------- 边界 6：回收槽位不推 stopping 帧 ----------
def _capture_publishes(monkeypatch, host):
    """记录 host._publish 推出去的事件（EventBuffer 是队列，不便回头翻）。"""
    seen: list[dict] = []
    real = host._publish

    def _spy(ev):
        seen.append(ev)
        return real(ev)

    host._publish = _spy
    return seen


@pytest.mark.asyncio
async def test_reclaiming_settled_idle_worker_does_not_emit_stopping():
    """干完活的空闲队友被回收时，前端不该先闪「停止中」再变「已完成」。

    它并没有被打断，只是槽位被收回；推 stopping 与事实不符。
    """
    host = _host()
    seen = _capture_publishes(None, host)
    node = host.registry.create_child(host.ctx, "keeper", team_id="team_x")
    node.status = "idle"
    node.rounds = 1
    node.result = "已完成"

    await host._stop_node(node, reason="close")

    statuses = [e.get("status") for e in seen if e.get("type") == "subagent_status"]
    assert "stopping" not in statuses, f"回收不该推 stopping，实际推了 {statuses}"
    assert node.status == "completed"


@pytest.mark.asyncio
async def test_stopping_frame_still_emitted_for_real_interrupt():
    """正向对照：真正打断一个在跑的队友，仍要推 stopping（面板不能一直「运行中」）。"""
    host = _host()
    seen = _capture_publishes(None, host)
    node = host.registry.create_child(host.ctx, "busy", team_id="team_x")
    node.status = "running"
    node.rounds = 1

    await host._stop_node(node, reason="close")

    statuses = [e.get("status") for e in seen if e.get("type") == "subagent_status"]
    assert "stopping" in statuses, f"真打断必须推 stopping，实际 {statuses}"


# ---------- 边界 2：idle 队友被 stop 时，idle_wait 要立刻返回 ----------
@pytest.mark.asyncio
async def test_stop_breaks_idle_wait_immediately():
    host = _host()
    n = host.registry.create_child(host.ctx, "w", team_id="team_x")
    n.status = "idle"
    n.idle_since = time.monotonic()
    n.wake.clear()

    async def _stop():
        await asyncio.sleep(0.05)
        n.stop.set()
        n.wake.set()

    asyncio.create_task(_stop())
    t0 = time.monotonic()
    reason = await host._idle_wait(n, timeout_s=10.0, poll_s=0.05)
    assert reason == "stopped"
    assert time.monotonic() - t0 < 1.0, "stop 不该等到超时"


# ---------- 边界 3：唤醒风暴——同一条消息多次投递不能重复入队 ----------
@pytest.mark.asyncio
async def test_repeated_delivery_of_same_message_id_does_not_duplicate():
    host = _host()
    n = host.registry.create_child(host.ctx, "w", team_id="team_x")
    n.status = "idle"
    msg = {"content": "改标题", "message_id": "t_same", "sender_agent_id": "root"}

    host.deliver_team_message("sub:w", msg)
    host.deliver_team_message("sub:w", msg)
    host.deliver_team_message("sub:w", msg)

    assert len(n.pending_messages) == 1, \
        f"同一 message_id 重复投递应去重，实际 {len(n.pending_messages)} 条"


# ---------- 边界 4：已有未消费消息时不能忙循环 ----------
@pytest.mark.asyncio
async def test_idle_wait_does_not_busy_loop_on_unconsumed_message():
    """一条一直没被消费的消息不该让 _idle_wait 每次轮询都返回 wake。

    真实循环里 _idle_wait 之后会 drain_pending 消费掉消息，所以在那里不会循环；
    但若把「有待处理」当唤醒条件，任何一次快速重新进入都会空转（实测 2s 34 次）。
    正确语义：只有「数量比进来时多了」才算新活。
    """
    host = _host()
    n = host.registry.create_child(host.ctx, "w", team_id="team_x")
    n.status = "idle"
    host.deliver_team_message("sub:w", {"content": "别丢", "message_id": "t_k"})
    n.wake.clear()

    reason = await host._idle_wait(n, timeout_s=0.15, poll_s=0.03)
    assert reason == "timeout", "已有消息不算新活，应正常超时而不是空转"
    assert len(n.pending_messages) == 1, "消息不能被吞掉"

    # 对照：等待期间又来了新消息 → 必须判定 wake
    n.wake.clear()
    async def _more():
        await asyncio.sleep(0.05)
        host.deliver_team_message("sub:w", {"content": "新的", "message_id": "t_k2"})
    asyncio.create_task(_more())
    reason2 = await host._idle_wait(n, timeout_s=1.0, poll_s=0.03)
    assert reason2 == "wake"
    assert len(n.pending_messages) == 2


# ---------- 边界 5：has_active 在 idle→running→idle 全过程中正确 ----------
@pytest.mark.asyncio
async def test_has_active_transitions_across_states():
    host = _host()
    n = host.registry.create_child(host.ctx, "w", team_id="team_x")
    n.status = "running"
    assert host.has_active() is True
    n.status = "idle"
    assert host.has_active() is False, "idle 不该拖住 SSE"
    assert host.has_live_nodes() is True
    n.status = "completed"
    assert host.has_active() is False
    assert host.has_live_nodes() is False


# ---------- 边界 6：wait 在 running→idle 时立刻返回 ready ----------
@pytest.mark.asyncio
async def test_wait_all_terminal_returns_ready_when_agent_goes_idle():
    """队友在跑时 wait 要等；跑完进 idle 后立刻返回 ready（不是等到超时）。"""
    host = _host()
    n = host.registry.create_child(host.ctx, "w", team_id="team_x")
    n.status = "running"

    async def _finish_soon():
        await asyncio.sleep(0.1)
        n.status = "idle"
        n.idle_since = time.monotonic()
        host._touch()

    asyncio.create_task(_finish_soon())
    t0 = time.monotonic()
    out = await host.wait(targets=["w"], condition="all_terminal", timeout_s=5.0)
    elapsed = time.monotonic() - t0

    assert out["timed_out"] is False
    assert out["state"] == "ready", f"进 idle 应返回 ready，实际 {out['state']}"
    assert elapsed < 1.0, f"应立刻返回，实际 {elapsed:.1f}s"


# ---------- 边界 6b：已 idle 但有待处理消息时，wait 不能判成已了结 ----------
@pytest.mark.asyncio
async def test_wait_does_not_settle_when_idle_has_pending_message():
    """消息刚投递、循环还没拉起时节点仍是 idle——此时 wait 不能提前返回。"""
    host = _host()
    n = host.registry.create_child(host.ctx, "w", team_id="team_x")
    n.status = "idle"
    host.deliver_team_message("sub:w", {"content": "新活", "message_id": "t_new"})
    assert n.is_idle is True and n.has_pending_work is True

    out = await host.wait(targets=["w"], condition="all_terminal", timeout_s=0.3)
    assert out["timed_out"] is True, "有待处理消息时不该判成 all_terminal"
    assert out["remaining_targets"] == ["w"]


# ---------- 边界 7：被回收的 idle 节点，其等待者必须被唤醒 ----------
@pytest.mark.asyncio
async def test_evicted_idle_node_wakes_its_waiters():
    reg = SubagentRegistry(max_depth=2, max_active=1)
    ctx = _ctx()
    a = reg.create_child(ctx, "a", team_id="team_x")
    a.status = "idle"
    a.idle_since = 1.0
    waiter = asyncio.Event()
    a.waiters.append(waiter)

    reg.create_child(ctx, "b", team_id="team_x")  # 触发淘汰 a
    assert a.stop.is_set()
    assert a.wake.is_set(), "被淘汰的节点要唤醒，避免 idle_wait 挂住"


# ---------- 边界 8：idle 期间累积多条消息，一次唤醒全部投递 ----------
@pytest.mark.asyncio
async def test_multiple_messages_accumulate_before_wake():
    host = _host()
    n = host.registry.create_child(host.ctx, "w", team_id="team_x")
    n.status = "idle"
    for i in range(3):
        host.deliver_team_message("sub:w", {"content": f"第{i}条", "message_id": f"t{i}"})
    assert len(n.pending_messages) == 3
    drained = n.drain_pending()
    assert len(drained) == 3
    assert n.pending_messages == []
    # 消费后再次 deliver 应该能重新唤醒
    n.wake.clear()
    host.deliver_team_message("sub:w", {"content": "新的", "message_id": "t9"})
    assert n.wake.is_set()


# ---------- 边界 9：close 一个 idle 节点后不可再投递 ----------
@pytest.mark.asyncio
async def test_closed_idle_node_rejects_delivery():
    host = _host()
    n = host.registry.create_child(host.ctx, "w", team_id="team_x")
    n.status = "idle"
    out = await host.close("w")
    assert out["ok"] is True
    # 关闭后再投递：应报错而不是静默写入
    res = host.deliver_team_message("sub:w", {"content": "晚了", "message_id": "t_late"})
    assert res["delivered"] is False


# ---------- 边界 10：idle_timeout=0 时不应进入 idle 分支 ----------
@pytest.mark.asyncio
async def test_zero_idle_timeout_skips_idle_branch():
    host = _host()
    host.idle_timeout_s = 0.0
    n = host.registry.create_child(host.ctx, "w", team_id="team_x")
    n.status = "idle"
    n.wake.clear()
    # idle_wait 本身仍可用（内部方法），但循环里不会走到
    reason = await host._idle_wait(n, timeout_s=0.05, poll_s=0.02)
    assert reason == "timeout"
