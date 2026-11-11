"""L3 韧性：lead 想收工时，团队还有活就不能结束这一轮。

真机 runId 111915-64a6：lead 说「等 reader」后结束了自己那一轮，
reader 的结论回到收件箱却没人接，任务 3 永久停在 pending，927 秒后用户
只拿到半句话。团队不能因为 lead 提前收工而退化。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent_loop import AgentLoop, _team_grace_from_env  # noqa: E402
from app.llm_client import ChatResult  # noqa: E402
from app.tools import ToolRegistry  # noqa: E402


class _Client:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def run(self, messages, tools=None, on_delta=None, stop_check=None, **kw):
        self.calls += 1
        text = self.replies.pop(0) if self.replies else "讲完了"
        if on_delta and text:
            on_delta("text", text)
        return ChatResult(content=text)


async def _drain(loop, history=None):
    out = []
    async for ev in loop.run_stream(history or [{"role": "user", "content": "干活"}]):
        out.append(ev)
    return out


@pytest.mark.asyncio
async def test_no_gate_keeps_old_behaviour(monkeypatch):
    """没有团队闸（普通对话）时行为完全不变：一次模型调用就收工。"""
    monkeypatch.delenv("HLZ_LEAD_TEAM_GRACE_TURNS", raising=False)
    client = _Client(["答案"])
    loop = AgentLoop(client, ToolRegistry(), "sys", max_turns=10)

    events = await _drain(loop)

    assert client.calls == 1, f"无闸时不该多重试，实际 {client.calls}"
    assert events[-1] == {"type": "done", "status": "complete"}


@pytest.mark.asyncio
async def test_gate_blocks_premature_finish(monkeypatch):
    """核心回归：队友还在跑时，模型想收工必须被拦住并续跑。"""
    monkeypatch.delenv("HLZ_LEAD_TEAM_GRACE_TURNS", raising=False)
    # 第一次问：队友还在跑；第二次问（续跑之后）：队友已经结束
    answers = ["还有 1 个队友正在运行（reader）。", ""]

    def _gate():
        return answers.pop(0) if answers else ""

    client = _Client(["我先等一下。", "拿到结果了，最终结论是 42。"])
    loop = AgentLoop(client, ToolRegistry(), "sys", max_turns=10, team_gate=_gate)

    events = await _drain(loop)

    assert client.calls == 2, f"应续跑一次再收工，实际 {client.calls}"
    kinds = [e["type"] for e in events]
    assert "team_wait" in kinds, f"应发出等待事件，实际 {kinds}"
    text = "".join(e.get("text", "") for e in events if e["type"] == "text_delta")
    assert "42" in text, f"续跑后应拿到最终结论，实际 {text!r}"


@pytest.mark.asyncio
async def test_gate_allows_finish_when_team_done(monkeypatch):
    """队友都结束了就立即收工，不能续跑（否则会话永远关不掉）。"""
    monkeypatch.delenv("HLZ_LEAD_TEAM_GRACE_TURNS", raising=False)
    client = _Client(["都完事了。"])
    loop = AgentLoop(client, ToolRegistry(), "sys", max_turns=10,
                     team_gate=lambda: "")

    events = await _drain(loop)

    assert client.calls == 1, f"没活就该收工，实际 {client.calls}"
    assert events[-1] == {"type": "done", "status": "complete"}


@pytest.mark.asyncio
async def test_gate_has_hard_limit(monkeypatch):
    """队友永远不结束（一直 idle）时，闸必须有硬上限，否则死循环。"""
    monkeypatch.setenv("HLZ_LEAD_TEAM_GRACE_TURNS", "2")
    client = _Client(["等", "还在等", "继续等", "等", "等", "等"])
    loop = AgentLoop(client, ToolRegistry(), "sys", max_turns=20,
                     team_gate=lambda: "还有 1 个队友在待命。")

    events = await _drain(loop)

    waits = [e for e in events if e["type"] == "team_wait"]
    assert len(waits) == 2, f"grace=2 应只续跑 2 次，实际 {len(waits)}"
    assert client.calls == 3, f"1 次初始 + 2 次续跑，实际 {client.calls}"
    assert events[-1]["type"] == "done", "到上限后必须收工"


@pytest.mark.asyncio
async def test_gate_exception_does_not_hang_the_turn(monkeypatch):
    """闸自己出错时不能让对话卡住：当作没有团队工作在跑来处理。"""
    monkeypatch.delenv("HLZ_LEAD_TEAM_GRACE_TURNS", raising=False)

    def _boom():
        raise RuntimeError("registry 已销毁")

    client = _Client(["收工"])
    loop = AgentLoop(client, ToolRegistry(), "sys", max_turns=10, team_gate=_boom)

    events = await _drain(loop)

    assert client.calls == 1
    assert events[-1] == {"type": "done", "status": "complete"}


@pytest.mark.asyncio
async def test_gate_ignored_when_user_stopped(monkeypatch):
    """用户点了停止：即使还有队友也不能续跑。"""
    monkeypatch.delenv("HLZ_LEAD_TEAM_GRACE_TURNS", raising=False)
    stop_event = asyncio.Event()

    class _C(_Client):
        async def run(self, messages, tools=None, on_delta=None, stop_check=None, **kw):
            self.calls += 1
            stop_event.set()
            if on_delta:
                on_delta("text", "停")
            return ChatResult(content="停")

    client = _C([])
    loop = AgentLoop(client, ToolRegistry(), "sys", max_turns=10,
                     team_gate=lambda: "还有队友在跑")

    await _drain(loop, )

    # 用真实 stop_event 再跑一次，确认不会被闸拉回去
    client2 = _C([])
    loop2 = AgentLoop(client2, ToolRegistry(), "sys", max_turns=10,
                      team_gate=lambda: "还有队友在跑")
    events = []
    async for ev in loop2.run_stream([{"role": "user", "content": "干活"}],
                                     stop_event=stop_event):
        events.append(ev)

    assert [e["type"] for e in events].count("team_wait") == 0
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_grace_env_is_clamped(monkeypatch):
    monkeypatch.setenv("HLZ_LEAD_TEAM_GRACE_TURNS", "999")
    assert _team_grace_from_env() == 10, "上限 10"
    monkeypatch.setenv("HLZ_LEAD_TEAM_GRACE_TURNS", "-5")
    assert _team_grace_from_env() == 0
    monkeypatch.setenv("HLZ_LEAD_TEAM_GRACE_TURNS", "x")
    assert _team_grace_from_env() == 3, "非法值回落默认"