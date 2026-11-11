"""Team 协作的硬制度：依赖不可绕过 + 消息必达 + 名字唯一解析。

这批测试对应 2026-11-11 真实 e2e 抓到的缺陷：
  D1 spawn_teammate 绑定任务绕过 blocked_by（只有 claim 校验）
  D2 同名 teammate 复用后 owner 绑到旧名字，teammate 无法 complete 自己的任务
  D3 lead 用裸名发消息，teammate 收件箱是 sub:<name>，消息永久读不到
  D4 消息只落库、从不投递，delivered_at / read_at 永远是 0
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, team_store  # noqa: E402
from app.scope import TeamScope  # noqa: E402
from app.subagent.context import AgentContext  # noqa: E402
from app.subagent.events import EventBuffer  # noqa: E402
from app.subagent.host import SubagentHost  # noqa: E402
from app.subagent.registry import SubagentRegistry  # noqa: E402
from app.team_host import TeamHost, TeamHostError  # noqa: E402


def _scope(*, agent_id: str = "root", team_id: str = "", role: str = "lead") -> TeamScope:
    uid = "u_" + uuid.uuid4().hex[:12]
    sid = "s_" + uuid.uuid4().hex[:12]
    return TeamScope(uid, sid, "g_" + uuid.uuid4().hex[:8],
                     team_id or ("team_" + uuid.uuid4().hex[:8]), agent_id, role=role)


@pytest_asyncio.fixture
async def lead():
    await db.init_db()
    await team_store.init_tables()
    scope = _scope()
    await team_store.create_team(scope, name="test")
    host = TeamHost(scope)
    yield host
    await team_store.delete_session_data(scope.user_id, scope.session_id)


# ---------------- D1：依赖必须在所有分配路径上生效 ----------------
@pytest.mark.asyncio
async def test_bind_task_owner_rejects_blocked_task(lead):
    """spawn_teammate 走的是 bind_task_owner；它必须和 claim 一样检查 blocked_by。"""
    a = await lead.create_task(subject="A")
    b = await lead.create_task(subject="B", blocked_by=[a["task_id"]])

    claim = await lead.claim(b["task_id"], expected_version=b["version"])
    assert claim["ok"] is False and claim["reason"] == "blocked"

    bind = await lead.bind_task_owner(
        b["task_id"], "sub:worker", expected_version=b["version"])
    assert bind["ok"] is False, "依赖未完成时不得把任务绑定给 teammate"
    assert bind.get("reason") == "blocked"

    task = await lead.get_task(b["task_id"])
    assert task["status"] == "pending"
    assert task["owner_agent_id"] == ""


@pytest.mark.asyncio
async def test_bind_task_owner_allowed_after_dependency_completes(lead):
    """正向对照：依赖完成后绑定必须成功，否则会把功能改坏。"""
    a = await lead.create_task(subject="A")
    b = await lead.create_task(subject="B", blocked_by=[a["task_id"]])
    ca = await lead.claim(a["task_id"], expected_version=a["version"])
    await lead.complete(a["task_id"], expected_version=ca["task"]["version"])

    bind = await lead.bind_task_owner(
        b["task_id"], "sub:worker", expected_version=b["version"])
    assert bind["ok"] is True
    assert bind["task"]["status"] == "in_progress"
    assert bind["task"]["owner_agent_id"] == "sub:worker"


@pytest.mark.asyncio
async def test_spawn_teammate_refuses_blocked_task(lead):
    """端到端：spawn_teammate 绑定被阻塞任务时必须拒绝，且不能留下半成品 owner。"""
    spawned = []

    async def fake_spawn(**kwargs):
        spawned.append(kwargs)
        return {"ok": True, "agent_id": "sub:" + kwargs["task_name"]}

    lead._spawn_fn = fake_spawn
    a = await lead.create_task(subject="A")
    b = await lead.create_task(subject="B", blocked_by=[a["task_id"]])

    out = await lead.spawn_teammate(
        task_name="worker", message="do B", team_task_id=b["task_id"],
        expected_version=b["version"])

    assert out["ok"] is False, "被依赖阻塞的任务不得被派活"
    assert not spawned, "拒绝时不能真的启动 teammate"
    task = await lead.get_task(b["task_id"])
    assert task["status"] == "pending" and task["owner_agent_id"] == ""


# ---------------- D3：收件人名字必须统一解析 ----------------
@pytest.mark.asyncio
async def test_recipient_bare_name_resolves_to_sub_prefixed_agent(lead):
    """lead 用裸名 poet 发消息，teammate（收件箱 sub:poet）必须能读到。"""
    await lead.send_message(recipient="poet", content="改标题")

    worker = TeamHost(TeamScope(lead.scope.user_id, lead.scope.session_id,
                                lead.scope.generation_id, lead.scope.team_id,
                                "sub:poet", role="teammate"))
    rows = await worker.list_messages()
    assert [r["content"] for r in rows] == ["改标题"], \
        "裸名与 sub: 前缀必须解析到同一个收件箱"


@pytest.mark.asyncio
async def test_recipient_sub_prefix_and_bare_name_share_one_inbox(lead):
    """两种写法发给同一个 agent，必须落在同一个收件箱里。"""
    await lead.send_message(recipient="poet", content="一")
    await lead.send_message(recipient="sub:poet", content="二")

    worker = TeamHost(TeamScope(lead.scope.user_id, lead.scope.session_id,
                                lead.scope.generation_id, lead.scope.team_id,
                                "sub:poet", role="teammate"))
    rows = await worker.list_messages()
    assert sorted(r["content"] for r in rows) == ["一", "二"]


@pytest.mark.asyncio
async def test_lead_aliases_are_normalized(lead):
    """lead / root / main / team-lead 都指向 root 的收件箱。"""
    worker = TeamHost(TeamScope(lead.scope.user_id, lead.scope.session_id,
                                lead.scope.generation_id, lead.scope.team_id,
                                "sub:poet", role="teammate"))
    for alias in ("lead", "root", "main", "team-lead"):
        await worker.send_message(recipient=alias, content=f"给 {alias}")
    rows = await lead.list_messages(recipient="root")
    assert len(rows) == 4, f"四个别名都应落到 root 收件箱，实际 {len(rows)}"


# ---------------- D4：投递状态机必须真的推进 ----------------
@pytest.mark.asyncio
async def test_message_starts_undelivered_then_marks_delivered(lead):
    """新消息 delivered_at=0；被读一次后必须变成已读，且不会重复投递。"""
    await lead.send_message(recipient="poet", content="hello")
    worker = TeamHost(TeamScope(lead.scope.user_id, lead.scope.session_id,
                                lead.scope.generation_id, lead.scope.team_id,
                                "sub:poet", role="teammate"))

    fresh = await worker.list_messages()
    assert len(fresh) == 1
    assert fresh[0]["delivered_at"] == 0 and fresh[0]["read_at"] == 0

    inbox = await worker.consume_inbox()
    assert [m["content"] for m in inbox] == ["hello"]

    after = await worker.list_messages()
    assert after[0]["read_at"] > 0, "投递成功后必须标记已读"
    assert await worker.consume_inbox() == [], "已投递的消息不能重复注入"


@pytest.mark.asyncio
async def test_consume_inbox_only_returns_own_messages(lead):
    """收件箱只能取到发给自己的，别人的消息一条都看不到。"""
    await lead.send_message(recipient="poet", content="给 poet")
    await lead.send_message(recipient="other", content="给 other")

    poet = TeamHost(TeamScope(lead.scope.user_id, lead.scope.session_id,
                              lead.scope.generation_id, lead.scope.team_id,
                              "sub:poet", role="teammate"))
    assert [m["content"] for m in await poet.consume_inbox()] == ["给 poet"]


# ---------------- D2：名字钉住，防止串名 ----------------
@pytest.mark.asyncio
async def test_name_pin_binds_recipient_to_first_agent(lead):
    """同名 agent 复用后，旧名字的消息不能落到新 agent 手里。"""
    await lead.pin_name("worker", "sub:worker")
    with pytest.raises(TeamHostError):
        await lead.pin_name("worker", "sub:worker_2")
    assert await lead.resolve_name("worker") == "sub:worker"


@pytest.mark.asyncio
async def test_resolve_name_defaults_to_sub_prefix(lead):
    assert await lead.resolve_name("poet") == "sub:poet"
    assert await lead.resolve_name("sub:poet") == "sub:poet"
    assert await lead.resolve_name("lead") == "root"


# ---------------- 发送给不存在的 agent 必须明确报错 ----------------
@pytest.mark.asyncio
async def test_send_to_unknown_agent_is_allowed_but_flagged(lead):
    """发给尚无 agent 的名字：消息仍要落库（可能稍后会有人用这个名字），
    但返回里必须标明当前没有活着的收件人，不能假装已送达。"""
    out = await lead.send_message(recipient="ghost", content="有人吗")
    assert out["recipient_agent_id"] == "sub:ghost"
    assert out.get("live") is False


@pytest.mark.asyncio
async def test_teammate_cannot_message_other_teammate(lead):
    """teammate 只能给 lead 发，不能互发（防止绕过 lead 私聊）。"""
    worker = TeamHost(TeamScope(lead.scope.user_id, lead.scope.session_id,
                                lead.scope.generation_id, lead.scope.team_id,
                                "sub:poet", role="teammate"))
    with pytest.raises(TeamHostError):
        await worker.send_message(recipient="other_worker", content="私聊")


@pytest.mark.asyncio
async def test_spawn_binds_task_to_actual_suffixed_agent_name(lead):
    """同名复用后，任务必须绑给真实 agent（worker_2），不是请求的名字。

    真实 e2e 里这一步错了会连锁：owner 是 sub:worker（已关闭），活着的
    sub:worker_2 调 complete 得到 not_found，任务只能由 lead 代收。
    """
    lead._spawn_fn = lambda **kw: _spawn_returning("worker_2")
    t = await lead.create_task(subject="second")
    out = await lead.spawn_teammate(
        task_name="worker", message="do", team_task_id=t["task_id"],
        expected_version=t["version"])

    assert out["ok"] is True
    assert out["task"]["owner_agent_id"] == "sub:worker_2", \
        "owner 必须是 spawn 实际返回的 agent_id"

    worker2 = TeamHost(TeamScope(lead.scope.user_id, lead.scope.session_id,
                                 lead.scope.generation_id, lead.scope.team_id,
                                 "sub:worker_2", role="teammate"))
    done = await worker2.complete(t["task_id"],
                                 expected_version=out["task"]["version"])
    assert done["ok"] is True, "真实 agent 必须能完成自己被绑定的任务"


async def _spawn_returning(name: str):
    return {"ok": True, "agent_id": "sub:" + name, "name": name}


@pytest.mark.asyncio
async def test_spawn_is_refused_before_starting_blocked_task(lead):
    """被阻塞的任务在 spawn 之前就被拒，不能先拉起 agent 再回滚。"""
    started = []

    async def spawn_fn(**kw):
        started.append(kw)
        return {"ok": True, "agent_id": "sub:" + kw["task_name"]}

    lead._spawn_fn = spawn_fn
    a = await lead.create_task(subject="A")
    b = await lead.create_task(subject="B", blocked_by=[a["task_id"]])
    out = await lead.spawn_teammate(
        task_name="w", message="m", team_task_id=b["task_id"],
        expected_version=b["version"])

    assert out["ok"] is False and out.get("reason") == "blocked"
    assert started == [], "被依赖阻塞时不应启动 teammate"
    assert (await lead.get_task(b["task_id"]))["owner_agent_id"] == ""


# ---------------- 子代理宿主侧的硬投递 ----------------
@pytest.mark.asyncio
async def test_host_delivers_message_to_live_agent_inbox(lead):
    """消息要真的进到活着的 agent 的待注入队列，而不是只写库。"""
    await db.init_db()
    ctx = AgentContext(user_id=lead.scope.user_id, session_id=lead.scope.session_id,
                       message_id="m1", generation_id=lead.scope.generation_id,
                       team_id=lead.scope.team_id, role="lead")

    class _FakeRuntime:
        registry = None

    host = SubagentHost(ctx, parent_runtime=_FakeRuntime(), buffer=EventBuffer())
    host.registry = SubagentRegistry(max_depth=2, max_active=4)
    node = host.registry.create_child(ctx, "poet", team_id=lead.scope.team_id)
    node.status = "running"

    delivered = host.deliver_team_message(
        "sub:poet", {"sender": "root", "content": "改标题", "message_id": "tmsg_1"})
    assert delivered["delivered"] is True
    assert delivered["name"] == "poet"

    pending = node.drain_pending()
    assert len(pending) == 1
    assert "改标题" in pending[0]["content"]


@pytest.mark.asyncio
async def test_host_refuses_delivery_to_dead_agent(lead):
    """发给已结束的 agent 必须明确失败，不能静默成功让消息消失。"""
    ctx = AgentContext(user_id=lead.scope.user_id, session_id=lead.scope.session_id,
                       message_id="m1", generation_id=lead.scope.generation_id,
                       team_id=lead.scope.team_id, role="lead")

    class _FakeRuntime:
        registry = None

    host = SubagentHost(ctx, parent_runtime=_FakeRuntime(), buffer=EventBuffer())
    host.registry = SubagentRegistry(max_depth=2, max_active=4)
    node = host.registry.create_child(ctx, "poet", team_id=lead.scope.team_id)
    node.status = "completed"

    out = host.deliver_team_message(
        "sub:poet", {"sender": "root", "content": "晚了", "message_id": "tmsg_2"})
    assert out["delivered"] is False
    assert "已结束" in out["reason"] or "closed" in out["reason"] or "终态" in out["reason"]


@pytest.mark.asyncio
async def test_lead_inbox_receives_teammate_messages(lead):
    """teammate 发给 lead 的消息必须进 lead 的待注入队列（否则本轮看不到）。"""
    ctx = AgentContext(user_id=lead.scope.user_id, session_id=lead.scope.session_id,
                       message_id="m1", generation_id=lead.scope.generation_id,
                       team_id=lead.scope.team_id, role="lead")

    class _FakeRuntime:
        registry = None

    host = SubagentHost(ctx, parent_runtime=_FakeRuntime(), buffer=EventBuffer())
    host.push_team_message({"sender": "sub:poet", "content": "任务完成", "kind": "result"})
    drained = host.drain_notifications()
    joined = "\n".join(m["content"] for m in drained)
    assert "任务完成" in joined
    assert "poet" in joined
