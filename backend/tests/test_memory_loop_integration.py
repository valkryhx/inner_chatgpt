"""L2 集成：记忆注入真的走过 AgentLoop 的轮循环。

单测（test_memory_inject.py）证明的是「注入器本身对」。这里证明的是
**它接在真实 `AgentLoop` 上时的行为**，尤其是三件单测原理上测不到的事：

1. 一个用户轮次触发多次模型调用时，同一批记忆只 append 一次（M-14）；
2. 注入块确实参与了模型调用（客户端看到的 messages 里有 `[记忆]`）；
3. 注入块不落 `session_history`（M-10），但当前轮的内存 history 里有。

用脚本化客户端而不是真 LLM：这里验证的是**编排**，不是模型能力。
真 LLM 的链路验证见 §7.2 的 S-M1/S-M2/S-M3，需要可用 key。
"""
from __future__ import annotations

import asyncio

import pytest

from app import memory_store as ms
from app.agent_loop import AgentLoop
from app.memory_inject import VOLATILE_KEY, install_memory_injector
from app.llm_client import ChatResult
from app.tools import Tool, ToolRegistry


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "MEMORIES_DIR", tmp_path / "memories")
    monkeypatch.setattr(ms, "DB_PATH", tmp_path / "app.db")
    run(ms.init_tables())
    return tmp_path


class ScriptedClient:
    """按脚本依次返回 ChatResult，并记录每次调用看到的 messages。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen: list[list[dict]] = []

    async def run(self, messages, tools=None, on_delta=None, stop_check=None, **kw):
        # 深拷贝一层：history 会被后续轮次继续 append，直接存引用会让断言看到未来状态
        self.seen.append([dict(m) for m in messages])
        item = self.script.pop(0) if self.script else ChatResult(content="完")
        if isinstance(item, ChatResult):
            if on_delta and item.content:
                on_delta("text", item.content)
            return item
        raise AssertionError(f"脚本没料了 / 意外项：{item}")


def _noop_tool() -> Tool:
    async def call(args):
        return "ok"
    return Tool("file_read", "读文件", {"type": "object", "properties": {}}, call)


async def _drain(loop, history):
    out = []
    async for ev in loop.run_stream(history):
        out.append(ev)
    return out


def _write(slug="prefs", description="用户希望被叫老徐", kind="user"):
    return run(ms.write_memory("u1", slug=slug, title=slug, description=description,
                               kind=kind, body="正文"))


def test_memory_reaches_model_and_is_not_persisted(store, tmp_path, monkeypatch):
    from app import history_store as hs
    monkeypatch.setattr(hs, "DB_PATH", tmp_path / "hist.db")
    hist = hs.HistoryStore()
    run(hist.init())

    _write()

    client = ScriptedClient([ChatResult(content="好的，老徐。")])
    loop = AgentLoop(client, ToolRegistry([_noop_tool()]), "sys", max_turns=3,
                     history_store=hist, user_id="u1")
    injector = install_memory_injector(loop, "u1", query_hint="你好")
    assert injector is not None

    history: list[dict] = [{"role": "user", "content": "你好"}]
    events = run(_drain(loop, history))

    # ① 模型真的看到了记忆块
    assert client.seen, "模型没被调用"
    first_msgs = client.seen[0]
    joined = "\n".join(str(m.get("content") or "") for m in first_msgs)
    assert "[记忆]" in joined
    assert "老徐" in joined

    # ② 内存 history 里也在（本轮模型上下文）
    assert any(VOLATILE_KEY in m for m in history)

    # ③ 持久化历史里没有
    persisted = run(hist.get("u1", ""))
    assert persisted, "历史没落盘"
    assert not any("[记忆]" in str(m.get("content") or "") for m in persisted)
    assert not any(VOLATILE_KEY in m for m in persisted)

    # ④ 事件流里发出了 message_injected（前端据此渲染）
    assert any(ev.get("type") == "message_injected" for ev in events)


def test_memory_injected_only_once_across_many_model_calls(store):
    """M-14：一个 generation 内 3 次模型调用，记忆块只 append 一次。"""
    _write()

    calls = {"n": 0}

    class MultiCallClient:
        """前两次调用返回工具调用（迫使循环继续），第三次收尾。"""

        async def run(self, messages, tools=None, on_delta=None, stop_check=None, **kw):
            calls["n"] += 1
            r = ChatResult(content="")
            if calls["n"] < 3:
                r.tool_calls = [{"id": f"c{calls['n']}", "type": "function",
                                 "function": {"name": "file_read", "arguments": "{}"}}]
            return r

    loop = AgentLoop(MultiCallClient(), ToolRegistry([_noop_tool()]), "sys",
                     max_turns=10, user_id="u1")
    install_memory_injector(loop, "u1")

    history: list[dict] = [{"role": "user", "content": "hi"}]
    events = run(_drain(loop, history))

    memory_blocks = [m for m in history if VOLATILE_KEY in m]
    assert len(memory_blocks) == 1, f"应只注入一次，实际 {len(memory_blocks)} 次"
    injected_events = [e for e in events if e.get("type") == "message_injected"]
    assert len(injected_events) == 1
    assert calls["n"] >= 2, "构造前提：至少两次模型调用"


def test_no_memory_means_no_injected_event(store):
    """负对照：没有任何记忆时不发 message_injected，也不往 history 塞东西。"""
    client = ScriptedClient([ChatResult(content="在")])
    loop = AgentLoop(client, ToolRegistry([_noop_tool()]), "sys", max_turns=3, user_id="u1")
    install_memory_injector(loop, "u1")
    history: list[dict] = [{"role": "user", "content": "hi"}]
    events = run(_drain(loop, history))
    assert not any(ev.get("type") == "message_injected" for ev in events)
    assert not any(VOLATILE_KEY in m for m in history)


def test_other_users_memory_never_reaches_model(store):
    """A 用户的记忆不该出现在 B 用户的模型输入里（负对照）。"""
    run(ms.write_memory("userA", slug="secret", title="secret",
                        description="A 的秘密是 1234", kind="user", body="x"))

    client = ScriptedClient([ChatResult(content="在")])
    loop = AgentLoop(client, ToolRegistry([_noop_tool()]), "sys", max_turns=3, user_id="userB")
    install_memory_injector(loop, "userB")
    run(_drain(loop, [{"role": "user", "content": "我的秘密是什么"}]))

    joined = "\n".join(str(m.get("content") or "") for m in client.seen[0])
    assert "1234" not in joined
    assert "[记忆]" not in joined


def test_disabled_env_means_no_injection(store, monkeypatch):
    _write()
    monkeypatch.setenv("HLZ_MEMORY_ENABLED", "0")
    client = ScriptedClient([ChatResult(content="在")])
    loop = AgentLoop(client, ToolRegistry([_noop_tool()]), "sys", max_turns=3, user_id="u1")
    assert install_memory_injector(loop, "u1") is None
    run(_drain(loop, [{"role": "user", "content": "hi"}]))
    assert "[记忆]" not in "\n".join(str(m.get("content") or "") for m in client.seen[0])


def test_new_memory_written_mid_turn_is_injected_next_model_call(store):
    """后台在轮中途写入新记忆 -> 下一次模型调用能注入它（不被布尔标志挡住）。"""
    _write(slug="aaa", description="第一条")

    calls = {"n": 0}

    class MidTurnClient:
        async def run(self, messages, tools=None, on_delta=None, stop_check=None, **kw):
            calls["n"] += 1
            r = ChatResult(content="")
            if calls["n"] == 1:
                r.tool_calls = [{"id": "c1", "type": "function",
                                 "function": {"name": "file_read", "arguments": "{}"}}]
            return r

    async def _tool(args):
        # 模拟「后台提取器在轮中途写了一条新记忆」
        await ms.write_memory("u1", slug="bbb", title="bbb",
                              description="第二条新记忆", kind="user", body="y")
        return "ok"

    reg = ToolRegistry([Tool("file_read", "读", {"type": "object", "properties": {}}, _tool)])
    loop = AgentLoop(MidTurnClient(), reg, "sys", max_turns=6, user_id="u1")
    install_memory_injector(loop, "u1")
    history: list[dict] = [{"role": "user", "content": "hi"}]
    run(_drain(loop, history))

    blocks = [m for m in history if VOLATILE_KEY in m]
    assert len(blocks) == 2, "第二条记忆应在下一次模型调用时补注入"
    assert "第二条新记忆" in blocks[1]["content"]
    assert "第一条" not in blocks[1]["content"], "已注入过的不该重复出现"
