"""持久化 worker（P-A）：状态语义拆分 + idle 循环 + 槽位回收。

对应设计文档 §4.5 的 7 处漏洞。这批测试先写、先红，再改实现。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, team_store  # noqa: E402
from app.scope import TeamScope  # noqa: E402
from app.subagent.context import AgentContext  # noqa: E402
from app.subagent.events import EventBuffer  # noqa: E402
from app.subagent.host import SubagentHost  # noqa: E402
from app.subagent.registry import AgentNode, SubagentRegistry  # noqa: E402


class _FakeRuntime:
    registry = None


def _ctx(team_id: str = "team_x") -> AgentContext:
    return AgentContext(user_id="u_" + uuid.uuid4().hex[:8],
                        session_id="s_" + uuid.uuid4().hex[:8],
                        message_id="m1", generation_id="g1",
                        team_id=team_id, role="lead")


def _host(ctx: AgentContext) -> SubagentHost:
    return SubagentHost(ctx, parent_runtime=_FakeRuntime(), buffer=EventBuffer())


# ---------------- 漏洞 1：wait(all_terminal) 不得被 idle 卡住 ----------------
@pytest.mark.asyncio
async def test_all_terminal_treats_idle_agent_as_settled():
    """队友处于 idle（等后续）时，wait(all_terminal) 必须立即返回，不能挂到超时。"""
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "idle"

    t0 = time.monotonic()
    out = await host.wait(targets=["worker"], condition="all_terminal", timeout_s=5.0)
    elapsed = time.monotonic() - t0

    assert out["timed_out"] is False, "idle 的队友不应让 all_terminal 超时"
    assert elapsed < 1.0, f"应立刻返回，实际等了 {elapsed:.1f}s"
    assert out["state"] in ("all_terminal", "ready")


@pytest.mark.asyncio
async def test_all_terminal_still_waits_for_running_agent():
    """正向对照：真正在跑的队友，all_terminal 仍应等（不能一律放行）。"""
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "running"

    out = await host.wait(targets=["worker"], condition="all_terminal", timeout_s=0.4)
    assert out["timed_out"] is True
    assert out["remaining_targets"] == ["worker"]


# ---------------- 漏洞 2：read_result 要能读 idle 队友 ----------------
@pytest.mark.asyncio
async def test_read_result_allows_idle_agent_with_partial_flag():
    """队友 idle 时，lead 需要读它上一轮的结论（标 partial）。"""
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "idle"
    node.result = "N = 76"

    out = await host.read_result("worker")
    assert out["ok"] is True, out
    assert out["result"] == "N = 76"
    assert out.get("partial") is True, "idle 结果必须标 partial，避免被当成最终结论"


@pytest.mark.asyncio
async def test_read_result_still_rejects_running_agent():
    """正向对照：真在跑的仍然拒绝，否则会读到半截内容。"""
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "running"
    out = await host.read_result("worker")
    assert out["ok"] is False


# ---------------- 漏洞 5：has_active 与 has_live_nodes 必须分开 ----------------
@pytest.mark.asyncio
async def test_has_active_ignores_idle_agents():
    """idle 且无活的队友不能拖住 SSE。"""
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "idle"
    assert host.has_active() is False
    assert host.has_live_nodes() is True, "但 close/stop 仍要能关掉它"


@pytest.mark.asyncio
async def test_has_active_true_for_running_agent():
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "running"
    assert host.has_active() is True


# ---------------- 漏洞 4：槽位回收要能淘汰 idle 节点 ----------------
@pytest.mark.asyncio
async def test_registry_evicts_longest_idle_before_terminal():
    reg = SubagentRegistry(max_depth=2, max_active=2)
    ctx = _ctx()
    a = reg.create_child(ctx, "a", team_id="team_x")
    a.status = "idle"
    a.idle_since = 100.0
    b = reg.create_child(ctx, "b", team_id="team_x")
    b.status = "idle"
    b.idle_since = 200.0

    assert len(reg.active_nodes()) == 2
    # 满了：再来一个必须淘汰「最久 idle」的 a（idle_since 最小）
    c = reg.create_child(ctx, "c", team_id="team_x")
    assert c.name == "c"
    assert reg.get("a") is None, "应淘汰最久空闲的 a"
    assert reg.get("b") is not None


# ---------------- 漏洞 6：followup 限流按未消费条数 ----------------
@pytest.mark.asyncio
async def test_followup_limit_is_per_pending_not_cumulative():
    """跨轮累计超过 5 条不应被封死；只有同一轮堆积 5 条才拒。"""
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "running"

    # 第 1..5 条：投递并立刻消费（模拟每轮都被吃掉）
    for i in range(5):
        out = await host.followup("worker", f"第{i}条")
        assert out["ok"] is True, out
        node.drain_pending()

    out = await host.followup("worker", "跨轮后的第6条")
    assert out["ok"] is True, f"跨轮累计不该被限流: {out}"

    # 同一轮堆积到 5 条后，第 6 条应被拒
    for i in range(5):
        node.pending_messages.append({"role": "user", "content": f"堆积{i}"})
    out = await host.followup("worker", "又一条")
    assert out["ok"] is False and "上限" in out["error"]


# ---------------- 漏洞 7：idle 通知与终态通知语义分开 ----------------
@pytest.mark.asyncio
async def test_idle_notification_is_distinct_from_end():
    """idle 帧不能让 lead 以为队友结束了——否则它会重复 spawn 一个还活着的 worker。"""
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "idle"
    node.result = "N = 76"
    host.push_idle_notification(node, idle_reason="available")
    joined = "\n".join(m["content"] for m in host.drain_notifications())

    assert "空闲通知" in joined, "idle 必须有自己的标题"
    assert "已经结束" not in joined, "不能声称它已结束"
    assert "已完成本轮" in joined, "要说清是「本轮完成」而不是「结束」"
    assert "send_message" in joined, "要告诉 lead 怎么继续用它"
    assert "N = 76" in joined, "要带上本轮结论"


@pytest.mark.asyncio
async def test_end_notification_still_says_ended():
    """正向对照：真正结束的队友，通知里仍要明确写「已结束」。"""
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "completed"
    node.result = "done"
    # 直接走 _notify（终态路径）
    import asyncio as _a
    _a.get_event_loop()
    host._notifications.append({
        "id": "n1", "agent": node.name, "status": "completed",
        "result_chars": 4, "preview": "done",
    })
    joined = "\n".join(m["content"] for m in host.drain_notifications())
    assert "已经结束" in joined
    assert "空闲通知" not in joined


# ---------------- 唤醒源 ----------------
@pytest.mark.asyncio
async def test_delivering_message_wakes_idle_agent():
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "idle"
    node.wake.clear()

    host.deliver_team_message("sub:worker", {"sender_agent_id": "root",
                                             "content": "继续", "message_id": "t1"})
    assert node.wake.is_set(), "投递消息必须唤醒 idle 的队友"
    assert len(node.pending_messages) == 1


@pytest.mark.asyncio
async def test_idle_wait_returns_on_wake():
    """idle 等待必须能被唤醒打断，而不是睡满整个超时。"""
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "idle"
    node.idle_since = time.monotonic()
    node.wake.clear()

    async def _wake_soon():
        await asyncio.sleep(0.1)
        node.wake.set()

    t0 = time.monotonic()
    asyncio.create_task(_wake_soon())
    reason = await host._idle_wait(node, timeout_s=5.0, poll_s=0.05)
    elapsed = time.monotonic() - t0

    assert reason == "wake"
    assert elapsed < 1.0, f"应被唤醒，实际等了 {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_idle_wait_times_out_without_wake():
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "idle"
    node.idle_since = time.monotonic()
    node.wake.clear()
    reason = await host._idle_wait(node, timeout_s=0.2, poll_s=0.05)
    assert reason == "timeout"


@pytest.mark.asyncio
async def test_idle_wait_returns_stopped_on_stop_signal():
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker", team_id="team_x")
    node.status = "idle"
    node.idle_since = time.monotonic()
    node.wake.clear()

    async def _stop_soon():
        await asyncio.sleep(0.1)
        node.stop.set()
        node.wake.set()

    asyncio.create_task(_stop_soon())
    reason = await host._idle_wait(node, timeout_s=5.0, poll_s=0.05)
    assert reason == "stopped"


# ---------------- 漏洞 3：run 进度需要可更新 ----------------
@pytest.mark.asyncio
async def test_update_run_progress_persists_without_finishing():
    await db.init_db()
    await team_store.init_tables()
    from app.subagent import store as sub_store

    run_id = "run_" + uuid.uuid4().hex[:10]
    uid, sid = "u_" + uuid.uuid4().hex[:8], "s_" + uuid.uuid4().hex[:8]
    await sub_store.create_run(
        run_id=run_id, ctx_user_id=uid, session_id=sid, message_id="m1",
        parent_agent_id="root", agent_id="sub:w", name="w", role="teammate",
        task_message="t", status="running", generation_id="g1", team_id="team_x")

    await sub_store.update_run_progress(run_id, turns=7, result_text="第一轮结论",
                                       user_id=uid, session_id=sid,
                                       generation_id="g1", team_id="team_x")
    row = await sub_store.get_run(run_id, user_id=uid, session_id=sid,
                                 generation_id="g1", team_id="team_x")
    assert row["status"] == "running", "进度更新不应把它标成终态"
    assert row["turns"] == 7
    assert "第一轮结论" in (row["result_text"] or "")

    await sub_store.finish_run(run_id, status="completed", result_text="最终",
                              turns=9, user_id=uid, session_id=sid,
                              generation_id="g1", team_id="team_x")
    row = await sub_store.get_run(run_id, user_id=uid, session_id=sid,
                                 generation_id="g1", team_id="team_x")
    assert row["status"] == "completed"
    assert "最终" in (row["result_text"] or "")
    await team_store.delete_session_data(uid, sid)


# ---------------- 漏洞 8：关闭「已干完活的空闲队友」不得记成 stopped ----------------
@pytest.mark.asyncio
async def test_close_idle_settled_worker_records_completed():
    """真实用法：lead 收完结果后 close_agent 释放槽位。

    它这一轮已经做完、结果也交回去了，被回收的只是槽位。记 stopped 会让前端
    显示「已停止」，用户会以为自己打断了它。
    """
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker_c", team_id="team_x")
    node.status = "idle"
    node.rounds = 1
    node.turns = 7
    node.result = "任务3：总结完成"

    out = await host.close("worker_c")

    assert out["ok"] is True
    assert node.status == "completed", f"应记 completed，实际 {node.status}"
    assert node.error == ""


@pytest.mark.asyncio
async def test_close_idle_worker_with_pending_message_stays_stopped():
    """反向确认：还有没处理的消息时关闭，是真的打断，不能被判成 completed。"""
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker_d", team_id="team_x")
    node.status = "idle"
    node.rounds = 1
    node.pending_messages.append({"content": "还没干的活", "message_id": "t1"})

    await host._stop_node(node)

    assert not node.is_terminal, "有待办消息时不能预判 completed"
    assert node.status == "stopping", f"应停在 stopping 交给循环判定，实际 {node.status}"


@pytest.mark.asyncio
async def test_close_running_worker_stays_stopped():
    """反向确认：正在跑的队友被关，不能被判成 completed。"""
    host = _host(_ctx())
    node = host.registry.create_child(host.ctx, "worker_e", team_id="team_x")
    node.status = "running"
    node.rounds = 1

    await host._stop_node(node)

    assert not node.is_terminal, "跑到一半时不能预判 completed"
    assert node.status == "stopping", f"应停在 stopping 交给循环判定，实际 {node.status}"


@pytest.mark.asyncio
async def test_slot_eviction_of_settled_idle_worker_records_completed():
    """槽位淘汰（max_active 打满）也不是「打断」，不该记 stopped。"""
    host = _host(_ctx())
    host.registry = SubagentRegistry(max_depth=2, max_active=1)
    a = host.registry.create_child(host.ctx, "a", team_id="team_x")
    a.status = "idle"
    a.idle_since = 1.0
    a.wake.clear()
    a.rounds = 1
    a.result = "已完成"

    host.registry.create_child(host.ctx, "b", team_id="team_x")

    assert a.stop.is_set(), "被淘汰的节点仍需 stop"
    assert a.status == "completed", f"淘汰已干完活的 idle 节点应记 completed，实际 {a.status}"
