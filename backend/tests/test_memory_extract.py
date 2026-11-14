"""M4 后半：后台提取编排（闸门 -> 派一次性的提取 loop -> 记录写入）。

与 ``test_memory_write_gates.py`` 的分工：
- 那边测**闸门判定**（纯函数）；
- 这边测**接线**：钩子是否 fire-and-forget、是否限并发、写的内容是否真落库、
  闭世界工具集是否只有 memory_write、失败是否不影响主对话。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import memory_extract as mx
from app import memory_store as ms
from app.llm_client import ChatResult


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "MEMORIES_DIR", tmp_path / "memories")
    monkeypatch.setattr(ms, "DB_PATH", tmp_path / "app.db")
    run(ms.init_tables())
    mx.reset_turn_counter()
    mx._inflight.clear()
    mx._background.clear()
    yield tmp_path
    mx.reset_turn_counter()
    mx._inflight.clear()
    mx._background.clear()


class FakeClient:
    """脚本化 LLM：按调用次序返回预置的 tool_calls；脚本耗尽即「不再调用工具」。

    比真 LLM 好用的地方：能精确构造「模型这次决定写 X」这种状态，
    而真模型无法稳定复现。真 LLM 的链路验证在 e2e 里做。

    脚本项是 tool_calls 列表；给空列表 = 这一轮模型决定不写任何东西，
    循环随即结束（否则 agent_loop 会一直跑下去）。
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list[dict]] = []

    async def run(self, messages, tools=None, on_delta=None, stop_check=None, **kw):
        self.calls.append([dict(m) for m in messages])
        step = self.script.pop(0) if self.script else []
        r = ChatResult(content="")
        if step:
            r.tool_calls = step
        return r


def _write_call(slug: str, description: str, content: str = "正文",
                call_id: str = "c1") -> dict:
    return {
        "id": call_id, "type": "function",
        "function": {
            "name": "memory_write",
            "arguments": json.dumps({
                "slug": slug, "title": slug, "description": description,
                "content": content, "kind": "user",
            }, ensure_ascii=False),
        },
    }


class FakeUR:
    def __init__(self, user_id: str, client):
        self.user_id = user_id
        self.model = type("M", (), {"client": client})()


# ---------------- 闭世界工具集 ----------------
def test_extract_registry_only_has_memory_write(store):
    rec = mx._WriteRecorder("u1")
    reg = mx.build_extract_registry("u1", rec)
    assert reg.names() == ["memory_write"], \
        f"提取器只该有 memory_write，实际 {reg.names()}"


def test_extract_registry_has_no_escape_hatches(store):
    """prompt 不是安全边界 —— 工具里没有 code_run / file_write / MCP 才是。"""
    reg = mx.build_extract_registry("u1", mx._WriteRecorder("u1"))
    for forbidden in ("code_run", "file_write", "file_patch", "file_read",
                      "list_files", "load_skill", "memory_list", "memory_read"):
        assert not reg.get(forbidden), f"提取器不该有 {forbidden}"


# ---------------- 提取本身 ----------------
def test_run_extraction_writes_memory(store):
    seen = "SENTINEL_X1"
    client = FakeClient([[_write_call("user-pref", f"用户希望被叫老徐 {seen}")]])
    out = run(mx.run_extraction(client, "u1",
                                f"用户：以后叫我老徐。{seen}\n助手：好的。"))
    assert out.ran and not out.error
    assert out.written == ["user-pref"]
    got = run(ms.get_memory("u1", "user-pref"))
    assert got is not None and seen in got.description


def test_run_extraction_records_only_successful_writes(store):
    """冲突/被拒的写入不算「写了」。"""
    # 先建一条，拿到版本
    first = run(ms.write_memory("u1", slug="p", description="v1", body="x"))
    # 模型这次带一个过期 if_version -> conflict
    bad_call = {
        "id": "c1", "type": "function",
        "function": {"name": "memory_write", "arguments": json.dumps({
            "slug": "p", "description": "v2", "content": "y",
            "if_version": first.spec.version if first.spec else "stale",
        }, ensure_ascii=False)},
    }
    # 先改一次让版本失效
    run(ms.write_memory("u1", slug="p", description="v1.5", body="x"))
    client = FakeClient([[bad_call]])
    out = run(mx.run_extraction(client, "u1", "用户：随便说点什么"))
    assert out.written == [], "冲突的写入不该被记成已写"


def test_run_extraction_isolated_per_user(store):
    client = FakeClient([[_write_call("only-a", "A 的偏好")]])
    run(mx.run_extraction(client, "userA", "用户：我的偏好是 X"))
    assert run(ms.get_memory("userA", "only-a")) is not None
    assert run(ms.get_memory("userB", "only-a")) is None


def test_run_extraction_timeout_is_bounded(store, monkeypatch):
    """上游卡住时必须有硬上限 —— 否则后台 task 会永远挂着。"""
    class HangClient:
        async def run(self, *a, **kw):
            await asyncio.sleep(30)
            return ChatResult(content="")

    monkeypatch.setattr(mx, "extract_timeout_s", lambda: 0.2)
    out = run(mx.run_extraction(HangClient(), "u1", "用户：x"))
    assert out.ran
    assert out.error and "超过" in out.error


def test_run_extraction_swallows_client_error(store):
    class BoomClient:
        async def run(self, *a, **kw):
            raise RuntimeError("上游炸了")

    out = run(mx.run_extraction(BoomClient(), "u1", "用户：x"))
    assert out.ran and out.error
    assert out.written == []


def test_extract_prompt_contains_conversation(store):
    client = FakeClient([[]])
    marker = "CONV_MARKER_9"
    run(mx.run_extraction(client, "u1", f"用户：请记住 {marker}"))
    assert marker in str(client.calls[0][0].get("content") or "")


# ---------------- 闸门接线 ----------------
def _history(n_latin: int = 40) -> list[dict]:
    return [{"role": "user", "content": " ".join(["word"] * n_latin)},
            {"role": "assistant", "content": "ok"}]


def test_maybe_extract_respects_auto_write_off(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_AUTO_WRITE", "0")
    client = FakeClient([[_write_call("x", "y")]])
    out = run(mx.maybe_extract(FakeUR("u1", client), _history()))
    assert out.ran is False
    assert "HLZ_MEMORY_AUTO_WRITE" in out.reason
    assert client.calls == [], "闸门没通过就不该调模型"


def test_maybe_extract_respects_hard_disable(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_AUTO_WRITE", "1")
    monkeypatch.setenv("HLZ_MEMORY_DISABLED", "1")
    client = FakeClient([[_write_call("x", "y")]])
    out = run(mx.maybe_extract(FakeUR("u1", client), _history()))
    assert out.ran is False and "HLZ_MEMORY_DISABLED" in out.reason
    assert client.calls == []


def test_maybe_extract_respects_frequency_gate(store, monkeypatch):
    """every_n=3：第 1、2 轮不跑，第 3 轮跑。"""
    monkeypatch.setenv("HLZ_MEMORY_AUTO_WRITE", "1")
    monkeypatch.setenv("HLZ_MEMORY_EVERY_N_TURNS", "3")
    # 第 3 轮才会真的调模型：写一条 -> 然后结束（空列表）
    client = FakeClient([[_write_call("a", "第一次")], []])
    ur = FakeUR("u1", client)

    r1 = run(mx.maybe_extract(ur, _history()))
    r2 = run(mx.maybe_extract(ur, _history()))
    r3 = run(mx.maybe_extract(ur, _history()))
    assert r1.ran is False and r2.ran is False
    assert r3.ran is True
    assert r3.written == ["a"]


def test_maybe_extract_respects_direct_write_gate(store, monkeypatch):
    """主代理本轮已经自己写过 -> 后台不重复提取。"""
    monkeypatch.setenv("HLZ_MEMORY_AUTO_WRITE", "1")
    monkeypatch.setenv("HLZ_MEMORY_EVERY_N_TURNS", "1")
    client = FakeClient([[_write_call("bg", "后台写的")]])
    history = _history() + [{
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c9", "type": "function",
                        "function": {"name": "memory_write", "arguments": "{}"}}],
    }]
    out = run(mx.maybe_extract(FakeUR("u1", client), history))
    assert out.ran is False and "直写" in out.reason
    assert client.calls == []


def test_maybe_extract_respects_prose_gate(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_AUTO_WRITE", "1")
    monkeypatch.setenv("HLZ_MEMORY_EVERY_N_TURNS", "1")
    monkeypatch.setenv("HLZ_MEMORY_MIN_PROSE_WORDS", "50")
    client = FakeClient([[_write_call("x", "y")]])
    out = run(mx.maybe_extract(FakeUR("u1", client), [{"role": "user", "content": "短"}]))
    assert out.ran is False and "自然语言" in out.reason


def test_maybe_extract_does_not_tick_counter_when_disabled(store, monkeypatch):
    """关掉再打开不该「补跑」一堆本该跳过的轮次。"""
    monkeypatch.setenv("HLZ_MEMORY_AUTO_WRITE", "0")
    client = FakeClient([])
    ur = FakeUR("u1", client)
    for _ in range(5):
        run(mx.maybe_extract(ur, _history()))
    assert mx._turn_counter("u1").n == 0, "关掉时不该消耗轮次计数"


# ---------------- 钩子：fire-and-forget ----------------
def test_hook_does_not_block_the_turn(store, monkeypatch):
    """钩子必须立刻返回 —— 否则 SSE 会卡在 done 之前，用户每轮等几十秒。"""
    monkeypatch.setenv("HLZ_MEMORY_AUTO_WRITE", "1")
    monkeypatch.setenv("HLZ_MEMORY_EVERY_N_TURNS", "1")

    class SlowClient:
        async def run(self, *a, **kw):
            await asyncio.sleep(2.0)
            return ChatResult(content="")

    async def scenario():
        hook = mx.make_turn_end_hook(FakeUR("u1", SlowClient()))
        started = asyncio.get_running_loop().time()
        await hook(_history())
        elapsed = asyncio.get_running_loop().time() - started
        # 立刻返回（不等那 2 秒）
        assert elapsed < 0.5, f"钩子阻塞了 {elapsed:.2f}s"
        await mx.drain_background(timeout_s=5.0)

    run(scenario())


def test_hook_snapshots_history(store, monkeypatch):
    """传给后台的必须是快照 —— 循环结束后 history 还会被继续 append。"""
    monkeypatch.setenv("HLZ_MEMORY_AUTO_WRITE", "1")
    monkeypatch.setenv("HLZ_MEMORY_EVERY_N_TURNS", "1")
    seen: list[str] = []

    class RecordingClient:
        async def run(self, messages, *a, **kw):
            seen.append(str(messages[0].get("content") or ""))
            return ChatResult(content="")

    async def scenario():
        history = _history(40)
        hook = mx.make_turn_end_hook(FakeUR("u1", RecordingClient()))
        await hook(history)
        # 钩子返回后立刻污染原 history（模拟下一轮 append）
        history.append({"role": "user", "content": "POLLUTED_LATER"})
        await mx.drain_background(timeout_s=5.0)

    run(scenario())
    assert seen, "提取没跑"
    assert "POLLUTED_LATER" not in seen[0], "后台读到了快照之后的消息"


def test_hook_limits_concurrency_per_user(store, monkeypatch):
    """同一用户最多一个提取在跑 —— 叠加只会浪费钱并互相冲突。"""
    monkeypatch.setenv("HLZ_MEMORY_AUTO_WRITE", "1")
    monkeypatch.setenv("HLZ_MEMORY_EVERY_N_TURNS", "1")
    calls = {"n": 0}

    class SlowClient:
        async def run(self, *a, **kw):
            calls["n"] += 1
            await asyncio.sleep(1.0)
            return ChatResult(content="")

    async def scenario():
        hook = mx.make_turn_end_hook(FakeUR("u1", SlowClient()))
        await hook(_history())   # 第一个开始跑
        await hook(_history())   # 第二个应被跳过（第一个还在飞）
        await hook(_history())
        await mx.drain_background(timeout_s=5.0)

    run(scenario())
    assert calls["n"] == 1, f"并发没被限制，跑了 {calls['n']} 次"


def test_hook_failure_does_not_raise(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_AUTO_WRITE", "1")
    monkeypatch.setenv("HLZ_MEMORY_EVERY_N_TURNS", "1")

    class BoomClient:
        async def run(self, *a, **kw):
            raise RuntimeError("炸")

    async def scenario():
        hook = mx.make_turn_end_hook(FakeUR("u1", BoomClient()))
        await hook(_history())     # 不能抛
        await mx.drain_background(timeout_s=5.0)

    run(scenario())


def test_hook_without_user_id_is_noop(store):
    class UR:
        user_id = ""
        model = None

    async def scenario():
        hook = mx.make_turn_end_hook(UR())
        await hook(_history())
        await mx.drain_background(timeout_s=1.0)

    run(scenario())
