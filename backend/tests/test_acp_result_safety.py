"""ga_result 的两条防事故规则（2026-11-11 真机事故后加）。

事故经过（session co_fa983ab2eef1，agent_id ga_c6a3683f）：

1. 模型调 ``ga_result(close=true, max_chars=8000, full=true)``，
   全文 13223 字符 → 截断到 8000，**同时 close 把会话销毁**；
2. 模型意识到读不全，再调 ``ga_result(max_chars=14500)``
   → ``KeyError: 未知的 GA 会话``，只拿到 43 字符的错误。

结果从未落盘，关闭即永久丢失。

两个修复：
- 规则 1：结果会被截断时拒绝 close（除非 force_close=true）；
- 规则 2：结果落库 ``ga_runs``，关闭后仍可读，返回 source="db"。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from app.acp.config import load_acp_settings
from app.acp.manager import AcpSessionManager, _DEFAULT_RESULT_MAX_CHARS
from app.subagent.context import AgentContext

FIXTURE = str(Path(__file__).parent / "fixtures" / "fake_ga_bridge.py")


class RecordingBuffer:
    def __init__(self):
        self.events = []

    def put(self, ev):
        self.events.append(ev)


def run(coro):
    return asyncio.run(coro)


def make(**extra):
    env = {
        "INNER_ACP_GA_COMMAND": json.dumps([sys.executable, FIXTURE]),
        "INNER_ACP_PROMPT_TIMEOUT_S": "30",
        "INNER_ACP_ENV_ALLOW": "FAKE_GA_MODE,FAKE_GA_LOG",
        **extra,
    }
    ctx = AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")
    return AcpSessionManager(ctx=ctx, buffer=RecordingBuffer(), settings=load_acp_settings(env))


@pytest.fixture(autouse=True)
def _ensure_ga_table():
    """每个用例前确保 ga_runs 存在（生产由 main.lifespan 建表）。"""
    from app.acp import store as ga_store

    run(ga_store.init_tables())
    yield


# ---------------- 规则 1：截断与关闭互斥 ----------------

def test_refuses_close_when_result_truncated(monkeypatch):
    """核心回归：会被截断时 close 必须被拒绝，不能静默销毁。"""
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr = make()
        try:
            out = await mgr.spawn(task="say hi")
            aid = out["agent_id"]
            await mgr.wait(aid, timeout_s=10)
            # max_chars=1 必然截断（"hello world" 有 11 字符）
            with pytest.raises(ValueError) as exc:
                await mgr.result(aid, max_chars=1, close=True)
            # 会话必须还在（拒绝关闭后不能顺手销毁）
            still_alive = aid in mgr._sessions
            return str(exc.value), still_alive
        finally:
            await mgr.close_all()

    msg, alive = run(scenario())
    assert "拒绝关闭" in msg
    assert "force_close" in msg, "错误信息要告诉调用方怎么绕过"
    assert alive is True, "拒绝关闭后会话必须仍然存在，否则等于换个方式销毁"


def test_truncation_alone_is_allowed(monkeypatch):
    """只截断不关闭：正常，允许（调用方自己决定下次再读）。"""
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr = make()
        try:
            out = await mgr.spawn(task="say hi")
            aid = out["agent_id"]
            await mgr.wait(aid, timeout_s=10)
            return await mgr.result(aid, max_chars=1)
        finally:
            await mgr.close_all()

    r = run(scenario())
    assert r["truncated"] is True
    assert r["result_chars"] == 11
    assert r["result"] == "h"


def test_force_close_bypasses_the_guard(monkeypatch):
    """逃生口：明确传 force_close=true 时才允许丢弃剩余部分。"""
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr = make()
        try:
            out = await mgr.spawn(task="say hi")
            aid = out["agent_id"]
            await mgr.wait(aid, timeout_s=10)
            r = await mgr.result(aid, max_chars=1, close=True, force_close=True)
            return r, (aid in mgr._sessions)
        finally:
            await mgr.close_all()

    r, alive = run(scenario())
    assert r["closed"] is True
    assert r["truncated"] is True
    assert alive is False, "force_close=true 必须真的关掉"


def test_close_without_truncation_still_works(monkeypatch):
    """默认上限足够时不截断，close 正常生效（不破坏既有用法）。"""
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr = make()
        try:
            out = await mgr.spawn(task="say hi")
            aid = out["agent_id"]
            await mgr.wait(aid, timeout_s=10)
            r = await mgr.result(aid, close=True)   # 用默认 max_chars
            return r, (aid in mgr._sessions)
        finally:
            await mgr.close_all()

    r, alive = run(scenario())
    assert r.get("truncated") is None, "11 字符不该被默认上限截断"
    assert r["closed"] is True
    assert r["result"] == "hello world"


# ---------------- 规则 2：关闭后仍可读 ----------------

def test_result_still_readable_after_close(monkeypatch):
    """核心回归：关闭后补读不能报 KeyError，要能拿到落库内容。"""
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr = make()
        try:
            out = await mgr.spawn(task="say hi")
            aid = out["agent_id"]
            await mgr.wait(aid, timeout_s=10)
            await mgr.result(aid, close=True)          # 读一次并关闭
            assert aid not in mgr._sessions
            return await mgr.result(aid)               # 关闭后再读
        finally:
            await mgr.close_all()

    r = run(scenario())
    assert r.get("source") == "db", "关闭后的读取必须标注来源为 db"
    assert r["result"] == "hello world", "落库内容必须能原样读回"
    assert r["agent_id"]
    assert r["status"] == "completed"


def test_persisted_full_text_is_readable(monkeypatch):
    """落库要保留全文，不只保留最终答复片段。"""
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr = make()
        try:
            out = await mgr.spawn(task="say hi")
            aid = out["agent_id"]
            await mgr.wait(aid, timeout_s=10)
            await mgr.result(aid, close=True)
            return await mgr.result(aid, full=True)
        finally:
            await mgr.close_all()

    r = run(scenario())
    assert r["source"] == "db"
    assert "hello world" in r["result"]


def test_unknown_agent_still_raises(monkeypatch):
    """完全没有记录的 agent_id 仍要报错，不能返回空结果。"""
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr = make()
        try:
            with pytest.raises(KeyError):
                await mgr.result("ga_does_not_exist")
        finally:
            await mgr.close_all()

    run(scenario())


# ---------------- 规则 3：close_all 也要落库 ----------------

def test_close_all_persists_running_session_result(monkeypatch):
    """回合结束时还没取过的 GA 会话，close_all 必须把结果落库。

    内部子代理有 finish_run 兜底，ACP 原先没有对应动作：
    close_all 直接 _entries.clear()，结果随内存一起消失，
    下一轮 ga_result 只能拿到 KeyError。
    """
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr = make()
        out = await mgr.spawn(task="say hi")
        aid = out["agent_id"]
        await mgr.wait(aid, timeout_s=10)
        # 故意**不**调 ga_result，直接走回合收尾
        await mgr.close_all()
        assert aid not in mgr._sessions, "close_all 之后内存会话应已清空"
        # 新 manager 模拟「下一轮」：内存里一定没有这条会话
        mgr2 = make()
        try:
            return await mgr2.result(aid)
        finally:
            await mgr2.close_all()

    r = run(scenario())
    assert r.get("source") == "db", "close_all 必须落库，否则下一轮读不到"
    assert r["result"] == "hello world"
    assert r["status"] == "completed"


def test_close_all_persists_full_text(monkeypatch):
    """close_all 落库要保留全文，供下一轮 full=true 读取。"""
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr = make()
        out = await mgr.spawn(task="say hi")
        aid = out["agent_id"]
        await mgr.wait(aid, timeout_s=10)
        await mgr.close_all()
        mgr2 = make()
        try:
            return await mgr2.result(aid, full=True)
        finally:
            await mgr2.close_all()

    r = run(scenario())
    assert r["source"] == "db"
    assert "hello world" in r["result"]


def test_close_all_records_stopped_status(monkeypatch):
    """close_all 先 stop 再落库：状态应是终态，不能存成 running。"""
    monkeypatch.setenv("FAKE_GA_MODE", "hang")

    async def scenario():
        mgr = make()
        out = await mgr.spawn(task="do something slow")
        aid = out["agent_id"]
        # 不等它完成，直接收尾（这就是「回合结束时还在跑」的场景）
        await mgr.close_all()
        mgr2 = make()
        try:
            return await mgr2.result(aid)
        finally:
            await mgr2.close_all()

    r = run(scenario())
    assert r["source"] == "db"
    assert r["status"] in ("stopped", "errored", "completed"), \
        f"必须是终态，实际 {r['status']}"
    assert r["status"] != "running", "落库不能存成 running（顺序错了）"


# ---------------- 默认上限 ----------------

def test_default_max_chars_matches_budget_layer():
    """默认上限与 tool_budget 对齐；太小会导致「明明能读全却读半截」。"""
    from app.tool_budget import DEFAULT_MAX_RESULT_CHARS

    assert _DEFAULT_RESULT_MAX_CHARS == DEFAULT_MAX_RESULT_CHARS
    assert _DEFAULT_RESULT_MAX_CHARS >= 50_000