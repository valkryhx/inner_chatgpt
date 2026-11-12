"""L2 韧性：模型调用彻底失败后，agent 循环能有限续跑。

真机 runId 111915-64a6：lead 的一次模型调用被上游切断，循环直接 yield error 结束，
它派出去的队友还在跑、任务板还有 pending，用户拿不到结论（927 秒，任务 3 永久 pending）。

调用层（llm_client）负责网络级重试；这一层负责「调用彻底失败后别让整轮死掉」。
续跑只重发模型调用，不重跑工具 —— 这是幂等性的关键。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent_loop import AgentLoop  # noqa: E402
from app.llm_client import ChatResult, LLMError  # noqa: E402
from app.tools import Tool, ToolRegistry  # noqa: E402


class _ScriptedClient:
    """按脚本依次返回结果或抛 LLMError。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def run(self, messages, tools=None, on_delta=None, stop_check=None, **kw):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(messages)
        if not isinstance(item, ChatResult):
            item = ChatResult(content=str(item))
        if on_delta and item.content:
            on_delta("text", item.content)
        return item


def _loop(script, *, max_turns=10, registry=None):
    reg = registry or ToolRegistry()
    return AgentLoop(_ScriptedClient(script), reg, "sys", max_turns=max_turns)


async def _drain(loop, history=None):
    out = []
    async for ev in loop.run_stream(history if history is not None else
                                    [{"role": "user", "content": "做点事"}]):
        out.append(ev)
    return out


@pytest.mark.asyncio
async def test_llm_error_then_success_continues_the_turn(monkeypatch):
    """核心回归：模型调用失败一次后，循环应续跑并拿到最终答案。"""
    monkeypatch.delenv("HLZ_AGENT_LLM_RETRIES", raising=False)
    loop = _loop([LLMError("network error: ReadError"), "最终答案"])

    events = await _drain(loop)

    kinds = [e["type"] for e in events]
    assert "llm_retry" in kinds, f"应发出续跑事件，实际 {kinds}"
    text = "".join(e.get("text", "") for e in events if e["type"] == "text_delta")
    assert text == "最终答案", f"续跑应产出答案，实际 {text!r}"
    assert "error" not in kinds, f"不该报错结束，实际 {kinds}"


@pytest.mark.asyncio
async def test_llm_error_gives_up_after_limit(monkeypatch):
    """续跑次数用尽就如实报错，不能无限续。"""
    monkeypatch.setenv("HLZ_AGENT_LLM_RETRIES", "2")
    loop = _loop([LLMError("boom"), LLMError("boom"), LLMError("boom"),
                  LLMError("boom"), LLMError("boom")])

    events = await _drain(loop)

    kinds = [e["type"] for e in events]
    assert kinds.count("llm_retry") == 2, f"应续跑 2 次，实际 {kinds.count('llm_retry')}"
    assert "error" in kinds, f"用尽后必须报错，实际 {kinds}"
    assert loop.client.calls == 3, f"1 次初始 + 2 次续跑，实际 {loop.client.calls}"


@pytest.mark.asyncio
async def test_continue_does_not_rerun_tools(monkeypatch):
    """幂等性核心：续跑只重发模型调用，已执行的工具绝不能再跑一遍。"""
    monkeypatch.delenv("HLZ_AGENT_LLM_RETRIES", raising=False)
    runs: list[str] = []

    async def _write(args):
        runs.append(args.get("v", ""))
        return "written"

    reg = ToolRegistry()
    reg.add(Tool("file_write", "写", {"type": "object", "properties": {"v": {"type": "string"}}},
                 _write))

    tool_call = ChatResult(content="", tool_calls=[
        {"id": "c1", "type": "function",
         "function": {"name": "file_write", "arguments": '{"v": "x"}'}}])
    loop = _loop([tool_call, LLMError("网络断了"), "完成"], registry=reg)

    events = await _drain(loop)

    assert runs == ["x"], f"工具只能执行一次，实际执行 {runs}"
    text = "".join(e.get("text", "") for e in events if e["type"] == "text_delta")
    assert text == "完成"
    assert "error" not in [e["type"] for e in events]


@pytest.mark.asyncio
async def test_no_continue_when_user_stopped(monkeypatch):
    """用户已经点停止：不能再续跑（否则停止后还在烧额度）。"""
    monkeypatch.delenv("HLZ_AGENT_LLM_RETRIES", raising=False)
    stop_event = asyncio.Event()

    class _Client:
        calls = 0

        async def run(self, messages, tools=None, on_delta=None, stop_check=None, **kw):
            _Client.calls += 1
            stop_event.set()  # 模拟「调用进行中用户点了停止」
            raise LLMError("boom")

    loop = AgentLoop(_Client(), ToolRegistry(), "sys", max_turns=5)
    events = []
    async for ev in loop.run_stream([{"role": "user", "content": "x"}],
                                    stop_event=stop_event):
        events.append(ev)

    kinds = [e["type"] for e in events]
    assert "llm_retry" not in kinds, f"停止后不该续跑，实际 {kinds}"
    assert _Client.calls == 1, f"停止后不该再发模型调用，实际 {_Client.calls} 次"
    assert "error" in kinds, f"应如实结束，实际 {kinds}"


@pytest.mark.asyncio
async def test_retry_limit_env_is_clamped(monkeypatch):
    from app.agent_loop import _llm_retry_limit_from_env

    monkeypatch.setenv("HLZ_AGENT_LLM_RETRIES", "99")
    assert _llm_retry_limit_from_env() == 3, "上限 3，防误配无限续跑"
    monkeypatch.setenv("HLZ_AGENT_LLM_RETRIES", "abc")
    assert _llm_retry_limit_from_env() == 2, "非法值回落默认"


@pytest.mark.asyncio
async def test_continue_injects_actionable_hint_not_raw_error(monkeypatch):
    """续跑提示要能指导模型继续，而不是把整段异常塞进上下文。"""
    monkeypatch.delenv("HLZ_AGENT_LLM_RETRIES", raising=False)
    seen_history = {}

    def _capture(msgs):
        seen_history["msgs"] = [m.get("content", "") for m in msgs]
        return "好了"

    loop = _loop([LLMError("HTTP 502: <html>bad gateway</html>"), _capture])
    await _drain(loop)

    joined = "\n".join(str(c) for c in seen_history.get("msgs", []))
    assert "接口中断" in joined, f"应注入可执行提示，实际 {joined[:300]}"
    assert "<html>" not in joined, "不该把原始上游 HTML 塞进上下文"

# ---------------- 用户可见的错误信息 ----------------
@pytest.mark.parametrize("raw,expect", [
    ("HTTP 502: <html><head><title>502 Bad Gateway</title></head>", "502"),
    ("HTTP 504: <html>Gateway Timeout</html>", "504"),
    ("HTTP 429: rate limited", "429"),
    ("network error: ReadError: connection reset", "连接中断"),
])
def test_llm_error_is_translated_for_users(raw, expect):
    """上游报错不该照搬给用户：502 的原文是整段 nginx HTML。"""
    from app.agent_loop import _friendly_llm_error

    msg = _friendly_llm_error(Exception(raw))

    assert expect in msg, f"应包含 {expect}，实际 {msg!r}"
    assert "<html>" not in msg, f"不该把 HTML 塞给用户，实际 {msg!r}"
    assert "重试" in msg or "重发" in msg, f"应告知已自动重试过，实际 {msg!r}"


def test_unknown_error_does_not_leak_whole_html():
    """未知错误也要截断，不能把整段 HTML 砸到界面上。"""
    from app.agent_loop import _friendly_llm_error

    big = "HTTP 418: " + "<div>" * 2000
    msg = _friendly_llm_error(Exception(big))

    assert len(msg) <= 320, f"应截断，实际长度 {len(msg)}"


class _Clock:
    """假时钟：首次读给起点，之后每次都跳到很远。

    不能简单地给 time.monotonic 整体加偏移 —— 起点也是在循环开头读的，
    两边同样被加，elapsed 恒为 0，闸门永远不会触发（第一版就是这么写错的）。
    """

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return 0.0 if self.reads == 1 else 10_000.0


@pytest.mark.asyncio
async def test_wall_clock_off_by_default(monkeypatch):
    """默认不限墙钟：normal 任务不该被兜底闸打断（保持历史行为）。"""
    monkeypatch.delenv("AGENT_WALL_CLOCK_S", raising=False)
    loop = _loop(["答案"])
    assert loop.wall_clock_s == 0.0
    events = await _drain(loop)
    assert not any(e["type"] == "error" for e in events)


@pytest.mark.asyncio
async def test_wall_clock_stops_the_turn(monkeypatch):
    """打开墙钟后，超限必须停下来，且不会再发起模型调用。"""
    import app.agent_loop as al

    loop = _loop(["甲", "乙", "丙"])
    loop.wall_clock_s = 60.0
    monkeypatch.setattr(al.time, "monotonic", _Clock())

    events = await _drain(loop)

    errs = [e for e in events if e["type"] == "error"]
    assert errs, f"超限必须报错，实际 {[e['type'] for e in events]}"
    assert "上限" in errs[0]["error"], errs[0]["error"]
    assert loop.client.calls == 0, f"超限后不该再调用模型，实际 {loop.client.calls}"


@pytest.mark.asyncio
async def test_wall_clock_error_names_the_env_var(monkeypatch):
    """报错必须告诉用户怎么调，否则他们只会觉得「又卡住了」。"""
    import app.agent_loop as al

    loop = _loop(["甲"])
    loop.wall_clock_s = 1.0
    monkeypatch.setattr(al.time, "monotonic", _Clock())

    events = await _drain(loop)

    err = next(e for e in events if e["type"] == "error")
    assert "AGENT_WALL_CLOCK_S" in err["error"], err["error"]
