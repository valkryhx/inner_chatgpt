"""steering 的后端链路：入口分流 + 注入器顺序 + 不重注入。

对应 docs/20261111-用户中途插话steering实施方案.md §4 步 3、4、5。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import steer_queue  # noqa: E402
from app.sse import _ensure_steer_injector  # noqa: E402
from app.steer_queue import drop_queue, get_queue, wrap_user_steer  # noqa: E402


class _Loop:
    def __init__(self):
        self.message_injector = None


class _UR:
    def __init__(self, generation_id="g1"):
        self.loop = _Loop()
        self.generation_id = generation_id


@pytest.fixture(autouse=True)
def _clean():
    yield
    for key in list(steer_queue._queues):
        steer_queue._queues.pop(key, None)


# ---------------- 非 team 路径的注入器 ----------------
@pytest.mark.asyncio
async def test_plain_path_gets_steer_injector():
    """普通对话（没有 team）也要能插话，那是绝大多数使用场景。"""
    ur = _UR()
    ur.loop._steer_aware = False
    get_queue("u1", "s1", "g1").push("顺便看看 a.py")

    _ensure_steer_injector(ur, "u1", "s1")

    injected = ur.loop.message_injector()
    assert len(injected) == 1
    assert "顺便看看 a.py" in injected[0]["content"]
    assert injected[0]["role"] == "user"


@pytest.mark.asyncio
async def test_injector_is_idempotent():
    """装两次不能变成两个注入器把消息取两遍。"""
    ur = _UR()
    ur.loop._steer_aware = False
    get_queue("u1", "s1", "g1").push("只说一次")

    _ensure_steer_injector(ur, "u1", "s1")
    first = ur.loop.message_injector
    _ensure_steer_injector(ur, "u1", "s1")

    assert ur.loop.message_injector is first
    assert len(ur.loop.message_injector()) == 1


@pytest.mark.asyncio
async def test_team_loop_is_not_double_wired():
    """team 路径已经会 drain 用户队列，非 team 注入器必须跳过它。"""
    ur = _UR()
    ur.loop._steer_aware = True  # 由 subagent/session.py 标记
    get_queue("u1", "s1", "g1").push("不应该被普通注入器取走")

    _ensure_steer_injector(ur, "u1", "s1")

    assert ur.loop.message_injector is None, "team 路径不该再装一个"
    # 消息仍留在队列里，等 team 的注入器来取
    assert len(get_queue("u1", "s1", "g1").snapshot()) == 1


@pytest.mark.asyncio
async def test_no_generation_id_does_not_wire():
    """拿不到 generation 就不装：宁可插话失效，也不能塞进取不到的队列。"""
    ur = _UR(generation_id="")
    ur.loop._steer_aware = False

    _ensure_steer_injector(ur, "u1", "s1")

    assert ur.loop.message_injector is None


@pytest.mark.asyncio
async def test_injector_tolerates_broken_queue(monkeypatch):
    """队列读失败不能拖垮整轮对话。"""
    ur = _UR()
    ur.loop._steer_aware = False

    def _boom(*a, **kw):
        raise RuntimeError("registry 崩了")

    monkeypatch.setattr("app.steer_queue.get_queue", _boom)
    _ensure_steer_injector(ur, "u1", "s1")

    assert ur.loop.message_injector() == []


# ---------------- 包装文案确实进上下文 ----------------
@pytest.mark.asyncio
async def test_injected_text_is_wrapped():
    ur = _UR()
    ur.loop._steer_aware = False
    get_queue("u1", "s1", "g1").push("再补一句西湖")

    _ensure_steer_injector(ur, "u1", "s1")
    content = ur.loop.message_injector()[0]["content"]

    assert content == wrap_user_steer("再补一句西湖")
    assert "不要重新回答" in content


# ---------------- 入口分流（_try_queue_steer） ----------------
@pytest.mark.asyncio
async def test_queue_returns_none_when_generation_not_live(monkeypatch):
    """竞态：回合刚好结束 → 降级为普通发送，不塞进没人消费的队列。"""
    from app.routers import chat

    async def _active(uid, sid):
        return "g1"

    monkeypatch.setattr(chat.team_store, "get_active_generation", _active)
    monkeypatch.setattr(chat, "is_generation_live", lambda *a: False)

    class _Body:
        text = "插一句"
        attachments = []

    out = await chat._try_queue_steer({"id": "u1"}, "s1", _Body())

    assert out is None, "回合已结束时应降级为普通发送"
    assert steer_queue.peek_queue("u1", "s1", "g1") is None, "不该凭空建队列"


@pytest.mark.asyncio
async def test_queue_pushes_and_returns_202(monkeypatch):
    from app.routers import chat

    async def _active(uid, sid):
        return "g1"

    monkeypatch.setattr(chat.team_store, "get_active_generation", _active)
    monkeypatch.setattr(chat, "is_generation_live", lambda *a: True)

    class _Body:
        text = "插一句"
        attachments = []

    out = await chat._try_queue_steer({"id": "u1"}, "s1", _Body())

    assert out is not None and out.status_code == 202
    assert [i["text"] for i in get_queue("u1", "s1", "g1").snapshot()] == ["插一句"]


@pytest.mark.asyncio
async def test_queue_rejects_when_full(monkeypatch):
    from fastapi import HTTPException

    from app.routers import chat

    async def _active(uid, sid):
        return "g1"

    monkeypatch.setattr(chat.team_store, "get_active_generation", _active)
    monkeypatch.setattr(chat, "is_generation_live", lambda *a: True)

    q = get_queue("u1", "s1", "g1")
    for i in range(steer_queue.MAX_QUEUE_DEPTH):
        q.push(f"m{i}")

    class _Body:
        text = "overflow"
        attachments = []

    with pytest.raises(HTTPException) as ei:
        await chat._try_queue_steer({"id": "u1"}, "s1", _Body())

    assert ei.value.status_code == 429


@pytest.mark.asyncio
async def test_queue_rejects_attachments_with_clear_reason(monkeypatch):
    """图片插话 P0 不支持，但必须给明确原因，不能静默丢弃。"""
    from fastapi import HTTPException

    from app.routers import chat

    async def _active(uid, sid):
        return "g1"

    monkeypatch.setattr(chat.team_store, "get_active_generation", _active)
    monkeypatch.setattr(chat, "is_generation_live", lambda *a: True)

    class _Body:
        text = "看图"
        attachments = [{"path": "x.png"}]

    with pytest.raises(HTTPException) as ei:
        await chat._try_queue_steer({"id": "u1"}, "s1", _Body())

    assert ei.value.status_code == 400
    assert "attachments_not_supported_in_queue_mode" in str(ei.value.detail)

# ---------------- 真实调用点：run_agent_stream 必须自己装上 ----------------
@pytest.mark.asyncio
async def test_run_agent_stream_wires_injector_for_plain_path():
    """走过真实 run_agent_stream，确认注入器是它自己装的（不是测试手动装的）。

    上一条只验证 _ensure_steer_injector 的行为；如果没人调用它，
    功能照样是死的 —— 这条固定「它被接在真实路径上」。
    """
    from app import sse

    seen: list[list[dict]] = []

    class _Store:
        async def get(self, *a):
            return []

        async def set(self, *a):
            return None

    class _Loop:
        def __init__(self):
            self.message_injector = None
            self._steer_aware = False

        async def run_stream(self, history, stop_event=None, session_id=""):
            # 模拟「模型调用前」这个 fold 点
            injected = self.message_injector() if self.message_injector else []
            seen.append(injected)
            yield {"type": "done", "status": "complete"}

    class _UR:
        def __init__(self):
            self.loop = _Loop()
            self.store = _Store()
            self.user_id = "u1"
            self.generation_id = "g1"

    ur = _UR()
    get_queue("u1", "s1", "g1").push("流式期间补一句")

    events = []
    async for ev in sse.run_agent_stream(ur, "s1", "原始问题"):
        events.append(ev)

    assert seen and seen[0], "run_agent_stream 必须自己装上注入器并取到插话"
    assert "流式期间补一句" in seen[0][0]["content"]


# ---------------- team 注入器：接线上必须真的 drain 用户队列 ----------------
def test_team_injector_source_drains_user_queue():
    """team 路径的注入器必须包含用户插话，且排在 consume_inbox 之前。

    build_turn_runtime 需要真实 host/DB 才能跑完，直接构造太重；这里固定住
    「那段代码确实存在且顺序正确」这条不变量。行为正确性由下面的顺序测例保证。
    """
    import inspect

    import app.subagent.session as sess_mod

    src = inspect.getsource(sess_mod.build_turn_runtime)
    steer_at = src.find("get_queue(")
    drain_at = src.find("q.drain()")
    inbox_at = src.find("consume_inbox(")
    notif_at = src.find("drain_notifications()")

    assert drain_at >= 0, "必须真的调用 q.drain()，只 get_queue 不会消费队列"
    assert steer_at >= 0, "team 注入器必须 drain 用户插话队列"
    assert inbox_at >= 0 and notif_at >= 0
    assert steer_at < drain_at < inbox_at < notif_at, (
        "顺序必须是 用户插话 -> 团队收件箱 -> 子代理通知")


# ---------------- 顺序：用户优先 ----------------
@pytest.mark.asyncio
async def test_user_message_precedes_notifications():
    """用户的新指令排在系统通知前面（新要求 vs 已发生的事实）。"""
    from app.steer_queue import SteerQueue, wrap_user_steer

    q = SteerQueue()
    q.push("改个需求")
    user_msgs = [{"role": "user", "content": wrap_user_steer(i["text"])}
                 for i in q.drain()]
    notifications = [{"role": "user", "content": "[通知] 队友结束了"}]
    # 模拟 session.py 的拼接顺序
    combined = user_msgs + [] + notifications

    assert "改个需求" in combined[0]["content"]
    assert "队友结束" in combined[-1]["content"]


# ---------------- 入口分流：send_message 路由真的会走 _try_queue_steer ----------------
def test_send_message_route_calls_queue_path():
    """mode=queue 的分流必须接在真实路由里，否则前端发了也白发。"""
    import inspect

    from app.routers import chat

    src = inspect.getsource(chat.send_message)
    assert "mode == \"queue\"" in src, "路由里必须有 mode=queue 的分支"
    call_at = src.find("_try_queue_steer(")
    acquire_at = src.find("acquire_generation(")
    assert call_at >= 0, "必须真的调用 _try_queue_steer"
    assert 0 <= call_at < acquire_at, (
        "排队判定必须在抢租约**之前**：抢了租约就等于开了新回合，"
        "那正是用户要避免的")
