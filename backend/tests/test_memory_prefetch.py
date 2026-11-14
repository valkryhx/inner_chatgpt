"""M3.5：LLM 相关性选择器 + 并发预取。

对应方案 §7.1 的 M-15 / M-16，外加选择器本身的解析、成本闸、防自激、
会话预算、去重。S-M6（命中率对比）/ S-M7（TTFT 对比）见
`tools/memory_verify/selector_bench.py` 与 `tests/e2e/memory_cases.py`。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import memory_prefetch as mp
from app import memory_store as ms
from app.llm_client import ChatResult


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "MEMORIES_DIR", tmp_path / "memories")
    monkeypatch.setattr(ms, "DB_PATH", tmp_path / "app.db")
    run(ms.init_tables())
    # 默认关；需要开的时候各测试自己 setenv
    monkeypatch.delenv("HLZ_MEMORY_SELECTOR", raising=False)
    monkeypatch.delenv("HLZ_MEMORY_SELECTOR_MODEL", raising=False)
    return tmp_path


def _mem(slug: str, desc: str, body: str = "", kind: str = "user"):
    return run(ms.write_memory("u1", slug=slug, title=slug, description=desc,
                               body=body or f"# {slug}\n\n{desc}\n", kind=kind))


class SelectorClient:
    """脚本化的选择器：记录收到的清单，返回指定 slug。"""

    def __init__(self, slugs: list[str], *, delay: float = 0.0):
        self.slugs = slugs
        self.delay = delay
        self.calls: list[list[dict]] = []

    async def run(self, messages, tools=None, on_delta=None, stop_check=None, **kw):
        self.calls.append([dict(m) for m in messages])
        if self.delay:
            await asyncio.sleep(self.delay)
        return ChatResult(content=json.dumps({"selected": self.slugs},
                                             ensure_ascii=False))


# ---------------- 配置闸 ----------------
def test_selector_off_by_default(store):
    assert mp.selector_enabled() is False
    assert mp.prefetch_enabled() is False


def test_enabled_requires_model(store, monkeypatch):
    """开着选择器但没指定模型 -> 整个机制不启用（不许回落主模型）。"""
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    assert mp.selector_enabled() is True
    assert mp.prefetch_enabled() is False, "没指定模型时不该启用"


def test_enabled_with_model(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small-model")
    assert mp.prefetch_enabled() is True


def test_hard_disabled_kills_prefetch(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    monkeypatch.setenv("HLZ_MEMORY_DISABLED", "1")
    assert mp.prefetch_enabled() is False


def test_memory_disabled_kills_prefetch(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    monkeypatch.setenv("HLZ_MEMORY_ENABLED", "0")
    assert mp.prefetch_enabled() is False


# ---------------- 解析 ----------------
def test_parse_filters_unknown_slugs(store):
    """模型编造的 slug 必须被丢掉 —— 否则读侧会出现幽灵引用。"""
    got = mp.parse_selector_output(
        json.dumps({"selected": ["a", "made-up", "b"]}), ["a", "b", "c"])
    assert got == ["a", "b"]


def test_parse_handles_markdown_fence(store):
    got = mp.parse_selector_output(
        '```json\n{"selected": ["a"]}\n```', ["a"])
    assert got == ["a"]


def test_parse_handles_prose_around_json(store):
    got = mp.parse_selector_output(
        '好的，我选这两条：{"selected": ["a", "b"]} 以上。', ["a", "b"])
    assert got == ["a", "b"]


def test_parse_bad_output_returns_empty(store):
    for bad in ("", "不是 JSON", "[]", '{"selected": "a"}', '{"selected": 5}'):
        assert mp.parse_selector_output(bad, ["a"]) == [], bad


def test_parse_respects_cap(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MAX", "2")
    got = mp.parse_selector_output(
        json.dumps({"selected": ["a", "b", "c", "d"]}), ["a", "b", "c", "d"])
    assert got == ["a", "b"]


def test_parse_dedupes(store):
    got = mp.parse_selector_output(
        json.dumps({"selected": ["a", "a", "b"]}), ["a", "b"])
    assert got == ["a", "b"]


# ---------------- 查询线索 ----------------
def test_last_user_text_skips_injected_blocks(store):
    """注入块本身就是 user role，不能拿它当下一轮的检索线索。

    否则第二轮是「用上一轮注入的记忆去检索记忆」，相关度越跑越偏。
    """
    from app.memory_inject import VOLATILE_KEY

    history = [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "回答"},
        {"role": "user", "content": "[记忆] 注入块", VOLATILE_KEY: True},
    ]
    assert mp.last_user_text(history) == "第一句"


def test_last_user_text_returns_latest(store):
    history = [{"role": "user", "content": "旧"},
               {"role": "assistant", "content": "x"},
               {"role": "user", "content": "新问题"}]
    assert mp.last_user_text(history) == "新问题"


def test_query_usable_rejects_tiny(store):
    assert mp.query_is_usable("") is False
    assert mp.query_is_usable("  ") is False
    assert mp.query_is_usable("a") is False
    assert mp.query_is_usable("ab") is True
    assert mp.query_is_usable("上次那个报错") is True


# ---------------- M-15：预取不阻塞首帧 ----------------
def test_m15_prefetch_does_not_block_first_token(store, monkeypatch):
    """选择器 sleep 500ms，但启动必须**立刻**返回（< 100ms）。

    这是整个 M3.5 的立身之本：合进注入器就退化成同步等待。
    """
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "关于 A 的描述")
    client = SelectorClient(["a"], delay=0.5)

    async def scenario():
        history = [{"role": "user", "content": "问一个具体的问题"}]
        started = time.monotonic()
        handle = mp.start_prefetch(None, history, client=client, user_id="u1",
                                   state=mp.PrefetchState())
        elapsed = time.monotonic() - started
        assert handle is not None
        assert elapsed < 0.1, f"start_prefetch 阻塞了 {elapsed:.3f}s"

        # 立刻 take：还没 settle -> 空（这就是「首轮不注入」）
        assert await handle.take(1) == []
        # 等它算完再 take -> 有内容
        await asyncio.wait_for(handle.promise, timeout=5)
        got = await handle.take(2)
        assert len(got) == 1 and "A" in got[0]["content"]
        return handle

    handle = run(scenario())
    assert handle.consumed_on_iteration == 2


# ---------------- M-16：跨轮消费 ----------------
def test_m16_take_before_settle_returns_empty(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "描述 A")
    client = SelectorClient(["a"], delay=0.3)

    async def scenario():
        handle = mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                                   client=client, user_id="u1",
                                   state=mp.PrefetchState())
        # 三轮都不该拿到东西（还没算完）
        assert await handle.take(1) == []
        assert await handle.take(2) == []
        return handle

    handle = run(scenario())
    assert handle.consumed_on_iteration == -1, "没 settle 时不该记成已消费"


def test_m16_consumed_once_then_never_again(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "描述 A")
    client = SelectorClient(["a"])

    async def scenario():
        handle = mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                                   client=client, user_id="u1",
                                   state=mp.PrefetchState())
        await asyncio.wait_for(handle.promise, timeout=5)
        first = await handle.take(1)
        second = await handle.take(2)
        third = await handle.take(3)
        return first, second, third, handle

    first, second, third, handle = run(scenario())
    assert len(first) == 1
    assert second == [], "同一句柄不该被消费两次"
    assert third == []
    assert handle.consumed_on_iteration == 1


def test_m16_hidden_by_first_iteration_metric(store, monkeypatch):
    """CC 的同类遥测：settle 了但首轮没赶上 = 预取白做了。"""
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "描述 A")

    async def scenario():
        # 首轮就没 settle
        slow = mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                                 client=SelectorClient(["a"], delay=0.3),
                                 user_id="u1", state=mp.PrefetchState())
        await asyncio.wait_for(slow.promise, timeout=5)
        # 首轮 take 发生在 settle 之前 -> 应记为「被首轮隐藏」
        return slow

    handle = run(scenario())
    assert handle.settled is True
    assert handle.hidden_by_first_iteration() is False, \
        "这轮是 settle 之后才 take 的，不算被隐藏"


# ---------------- 去重：slug 集合 ----------------
def test_slug_dedup_across_calls(store, monkeypatch):
    """同一 slug 不会被注入两次；新出现的 slug 仍能进来。

    布尔标志会让「第三轮新写的 B」永远注入不进去 —— slug 集合没这个问题。
    """
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MAX_CALLS", "10")
    _mem("a", "描述 A")
    _mem("b", "描述 B")
    state = mp.PrefetchState()

    async def one(slugs):
        h = mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                              client=SelectorClient(slugs), user_id="u1",
                              state=state)
        await asyncio.wait_for(h.promise, timeout=5)
        return await h.take(1)

    first = run(one(["a"]))
    assert len(first) == 1 and "A" in first[0]["content"]
    # 再来一次选同一批 -> 空（已注入过）
    second = run(one(["a"]))
    assert second == [], "同一 slug 不该注入两次"
    # 换成 b -> 能进（证明不是布尔标志）
    third = run(one(["a", "b"]))
    assert len(third) == 1 and "B" in third[0]["content"]


# ---------------- 成本闸 ----------------
def test_max_calls_per_generation(store, monkeypatch):
    """每代选择器调用次数有上限 —— 一个 10 步任务不该跑 10 次。"""
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MAX_CALLS", "2")
    _mem("a", "描述 A")
    history = [{"role": "user", "content": "具体问题"}]

    async def scenario():
        state = mp.PrefetchState()
        handles = []
        for _ in range(5):
            h = mp.start_prefetch(None, history, client=SelectorClient(["a"]),
                                  user_id="u1", state=state)
            handles.append(h)
            if h is not None:
                await asyncio.wait_for(h.promise, timeout=5)
        return handles

    handles = run(scenario())
    made = [h for h in handles if h is not None]
    assert len(made) == 2, f"调用上限 2，实际启动 {len(made)} 次"


def test_max_calls_zero_disables(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MAX_CALLS", "0")
    _mem("a", "描述 A")

    async def scenario():
        return mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                                 client=SelectorClient(["a"]), user_id="u1",
                                 state=mp.PrefetchState())

    assert run(scenario()) is None


# ---------------- 防自激 ----------------
def test_no_self_trigger_for_known_sources(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "描述 A")

    async def scenario():
        for src in ("extract_memories", "auto_dream", "memory_selector", "compaction"):
            h = mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                                  query_source=src,
                                  client=SelectorClient(["a"]), user_id="u1",
                                  state=mp.PrefetchState())
            assert h is None, f"{src} 不该触发选择器"

    run(scenario())


def test_normal_source_is_allowed(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "描述 A")

    async def scenario():
        h = mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                              query_source="user_input",
                              client=SelectorClient(["a"]), user_id="u1",
                              state=mp.PrefetchState())
        assert h is not None
        await asyncio.wait_for(h.promise, timeout=5)
        return h

    run(scenario())


# ---------------- 会话字节预算 ----------------
def test_session_budget_blocks_further_recall(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    monkeypatch.setenv("HLZ_MEMORY_SESSION_MAX_BYTES", "50")
    _mem("a", "描述 " + "很长" * 40)
    state = mp.PrefetchState()

    async def one():
        h = mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                              client=SelectorClient(["a"]), user_id="u1",
                              state=state)
        if h is None:
            return None
        await asyncio.wait_for(h.promise, timeout=5)
        return await h.take(1)

    first = run(one())
    assert first and len(first) == 1
    assert state.budget.exhausted(), "超预算后应标记为耗尽"
    assert run(one()) is None, "预算耗尽后不该再启动预取"


def test_session_budget_zero_means_unlimited(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    monkeypatch.setenv("HLZ_MEMORY_SESSION_MAX_BYTES", "0")
    _mem("a", "很长的描述" * 50)
    state = mp.PrefetchState()

    async def one():
        h = mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                              client=SelectorClient(["a"]), user_id="u1",
                              state=state)
        await asyncio.wait_for(h.promise, timeout=5)
        return await h.take(1)

    run(one())
    assert state.budget.exhausted() is False, "0 应该表示不限"


# ---------------- 失败退化 ----------------
def test_selector_failure_degrades_to_empty(store, monkeypatch):
    """选择器炸了 -> 本轮不注入（不是整轮失败）。"""
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "描述 A")

    class Boom:
        async def run(self, *a, **kw):
            raise RuntimeError("选择器炸了")

    async def scenario():
        h = mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                              client=Boom(), user_id="u1",
                              state=mp.PrefetchState())
        await asyncio.wait_for(asyncio.shield(h.promise), timeout=5)
        return await h.take(1)

    assert run(scenario()) == []


def test_selector_timeout_does_not_hang(store, monkeypatch):
    """选择器卡住 -> 按超时返回空，不挂住。"""
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_TIMEOUT_S", "0.2")
    _mem("a", "描述 A")

    class Hang:
        async def run(self, *a, **kw):
            await asyncio.sleep(30)
            return ChatResult(content="")

    async def scenario():
        h = mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                              client=Hang(), user_id="u1",
                              state=mp.PrefetchState())
        started = time.monotonic()
        await asyncio.wait_for(h.promise, timeout=5)
        assert time.monotonic() - started < 2.0
        return await h.take(1)

    assert run(scenario()) == []


# ---------------- 选择器看到的输入 ----------------
def test_selector_sees_清单_and_query(store, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "构建失败的处理方式 BUILD_FAIL")
    client = SelectorClient(["a"])

    async def scenario():
        h = mp.start_prefetch(None, [{"role": "user", "content": "上次那个报错"}],
                              client=client, user_id="u1",
                              state=mp.PrefetchState())
        await asyncio.wait_for(h.promise, timeout=5)
        await h.take(1)

    run(scenario())
    assert client.calls, "选择器没被调用"
    prompt = client.calls[0][-1]["content"]
    assert "BUILD_FAIL" in prompt, "清单里应含描述"
    assert "上次那个报错" in prompt, "应含本轮查询"


def test_selector_prompt_suppresses_keyword_collision(store):
    """系统提示词必须显式压制「字面撞词」。这是 CC 的硬约束之一。"""
    p = mp.SELECTOR_PROMPT
    assert "字面撞词" in p or "撞词" in p
    assert "最多" in p and "5" in p


def test_selected_block_has_disclaimer(store, monkeypatch):
    """首句免责是硬约束（CC `Zoo.relevant_memories` 第一句）。

    没有它，模型倾向于把召回结果当既定事实全盘套用。
    """
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "描述 A")

    async def scenario():
        h = mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                              client=SelectorClient(["a"]), user_id="u1",
                              state=mp.PrefetchState())
        await asyncio.wait_for(h.promise, timeout=5)
        return await h.take(1)

    got = run(scenario())
    assert got and "仅为" in got[0]["content"] and "可能" in got[0]["content"]
    assert got[0].get("_volatile") is True, "注入块必须带 _volatile（不落历史）"


# ---------------- AgentLoop 接线 ----------------
def test_loop_consumes_only_after_tool_steps(store, monkeypatch):
    """挂点验证：预取在**工具步骤之后**才被消费。

    用 FakeClient 让模型第一轮就要求一个工具，第二轮结束。预取内容必须
    出现在工具结果之后、第二轮流开始之前。
    """
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "描述 A_SENTINEL")

    from app.agent_loop import AgentLoop
    from app.tools import Tool, ToolRegistry

    async def echo(args):
        return "tool-out"

    reg = ToolRegistry([Tool("echo", "echo", {"type": "object", "properties": {}}, echo)])

    class Model:
        def __init__(self):
            self.n = 0
            self.seen: list[list[dict]] = []

        async def run(self, messages, tools=None, on_delta=None, stop_check=None, **kw):
            self.seen.append([dict(m) for m in messages])
            # 真模型调用是秒级；这里给 50ms 让预取协程有机会 settle，
            # 否则测的是「瞬时返回」这个不真实的情形。
            await asyncio.sleep(0.05)
            self.n += 1
            if self.n == 1:
                return ChatResult(content="", tool_calls=[{
                    "id": "c1", "type": "function",
                    "function": {"name": "echo", "arguments": "{}"}}])
            return ChatResult(content="done")

    model = Model()
    loop = AgentLoop(model, reg, "sys", max_turns=5, user_id="u1")
    state = mp.PrefetchState()

    def factory(_loop, history):
        return mp.start_prefetch(_loop, history, client=SelectorClient(["a"]),
                                 user_id="u1", state=state)
    loop.prefetch_factory = factory

    async def scenario():
        history = [{"role": "user", "content": "问一个具体问题"}]
        evs = [e async for e in loop.run_stream(history)]
        return history, evs

    history, evs = run(scenario())
    injected = [e for e in evs if e.get("type") == "message_injected"]
    assert injected, "预取内容没被注入"
    assert "A_SENTINEL" in injected[0]["text"]
    # 第一次模型调用不该看到记忆（那时选择器还没 settle）
    assert not any("A_SENTINEL" in str(m.get("content") or "") for m in model.seen[0])
    # 第二次能看到
    assert any("A_SENTINEL" in str(m.get("content") or "") for m in model.seen[1])


def test_shared_slugs_between_injector_and_prefetch(store, monkeypatch):
    """关键词注入器与选择器预取必须共用同一个去重集合。

    2026-11-14 真机 e2e 发现的缺陷：两条路径各记一份 ``_injected_slugs``，
    同一条记忆会既以 ``[记忆]`` 又以 ``[记忆·相关]`` 注入两次（内容完全相同）。
    本地单测当时全绿 —— 因为测的是各自路径，没人测「两条路径同时开」。

    ⚠️ 现在两条路径是**互斥**的（选择器开则关键词让位，见
    ``test_keyword_and_selector_are_mutually_exclusive``）。所以这里
    **直接构造 MemoryInjector 并手动调用**，绕过互斥开关 —— 测的是
    「共享集合真的被共用」这个机制本身，而不是两条路径能不能同时跑。
    即使将来改回叠加模式，这个保证也必须成立。

    注意仍要开选择器环境变量：``start_prefetch`` 自己会检查
    ``prefetch_enabled()``，不开就返回 None。

    又因为互斥生效时 ``MemoryInjector.__call__`` 直接返回空，这里**不调它**，
    而是手动往共享集合里放一个 slug —— 模拟「关键词路径已经注入过 a」。
    测的是集合身份共享，不是注入器本身。
    """
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "描述 A_SENTINEL")

    from app.memory_prefetch import PrefetchState

    shared: set[str] = {"a"}   # 关键词路径已注入过 a
    state = PrefetchState(shared_slugs=shared)
    assert state.injected is shared, "预取必须共用关键词注入器的集合"

    async def scenario():
        h = mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                              client=SelectorClient(["a"]), user_id="u1",
                              state=state)
        await asyncio.wait_for(h.promise, timeout=5)
        return await h.take(1)

    assert run(scenario()) == [], "关键词已注入过的 slug，选择器不该再注入"


def test_injector_after_prefetch_sees_selector_slugs(store, monkeypatch):
    """反向：选择器先注入的，关键词路径也不该再注入。

    与上一条同理：直接检查集合被选择器写入，而不是调 ``MemoryInjector.__call__``
    （互斥时它直接返回空，测不到东西）。
    """
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "描述 A_SENTINEL")

    shared: set[str] = set()
    state = mp.PrefetchState(shared_slugs=shared)

    async def scenario():
        h = mp.start_prefetch(None, [{"role": "user", "content": "具体问题"}],
                              client=SelectorClient(["a"]), user_id="u1",
                              state=state)
        await asyncio.wait_for(h.promise, timeout=5)
        return await h.take(1)

    assert len(run(scenario())) == 1
    assert shared == {"a"}, "选择器写入的 slug 必须进共享集合"


def test_prefetch_state_reset_each_generation(store, monkeypatch):
    """每代重置：上一代的去重集合不能让这一代的记忆注入不进来。"""
    _mem("a", "描述 A")

    from app.memory_inject import MemoryInjector

    # 模拟两代：每代新建集合与注入器
    shared1: set[str] = set()
    inj1 = MemoryInjector("u1", injected_slugs=shared1)
    assert len(run(inj1())) == 1

    shared2: set[str] = set()
    inj2 = MemoryInjector("u1", injected_slugs=shared2)
    assert shared2 is not shared1, "第二代必须换新集合"
    assert len(run(inj2())) == 1, "第二代应能重新注入"


def test_install_creates_fresh_set_each_call(store, monkeypatch):
    """`install_memory_injector` 每次调用都换新集合（loop 跨代复用）。"""
    _mem("a", "描述 A")

    from app.memory_inject import install_memory_injector

    class FakeLoop:
        message_injector = None

    loop = FakeLoop()
    install_memory_injector(loop, "u1")
    first = loop._memory_injected_slugs
    install_memory_injector(loop, "u1")
    assert loop._memory_injected_slugs is not first


def test_keyword_and_selector_are_mutually_exclusive(store, monkeypatch):
    """选择器开启时关键词注入器**让位**（两者互斥，不是叠加）。

    依据方案 §3.7：「选择器是工具存储 / MCP 记忆库形态的配套，与 AutoMem 互斥」。

    为什么必须互斥（真机实测得出）：关键词注入器在首轮模型调用**之前**同步跑，
    选择器预取要到工具步骤**之后**才消费。两条同时开时关键词必定先抢走 slug，
    选择器算完发现全被占用 —— 白跑一次模型调用，S-M6 量出的 37.5% 命中率
    优势在生产链路上一次也体现不出来。
    """
    from app.memory_inject import keyword_injection_enabled

    _mem("a", "描述 A")
    # 选择器关：关键词路径生效
    assert keyword_injection_enabled() is True
    # 选择器开（且模型已指定）：关键词路径让位
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    assert keyword_injection_enabled() is False


def test_keyword_still_works_when_selector_configured_but_model_missing(store, monkeypatch):
    """只开了 SELECTOR 但没指定模型 = 选择器未启用 -> 关键词路径仍生效。

    否则用户会陷入「记忆完全不注入」而不知道为什么。
    """
    from app.memory_inject import keyword_injection_enabled

    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.delenv("HLZ_MEMORY_SELECTOR_MODEL", raising=False)
    assert keyword_injection_enabled() is True


def test_selector_path_injects_when_keyword_yields(store, monkeypatch):
    """互斥的完整效果：选择器开启后，单轮只看到一份注入（不是两份）。"""
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "描述 A_SENTINEL")

    from app.memory_inject import install_memory_injector

    class FakeLoop:
        message_injector = None

    loop = FakeLoop()
    injector = install_memory_injector(loop, "u1")
    # 关键词路径让位
    assert run(injector()) == []

    state = mp.PrefetchState(shared_slugs=loop._memory_injected_slugs)

    async def scenario():
        h = mp.start_prefetch(loop, [{"role": "user", "content": "具体问题"}],
                              client=SelectorClient(["a"]), user_id="u1",
                              state=state)
        await asyncio.wait_for(h.promise, timeout=5)
        return await h.take(1)

    got = run(scenario())
    assert len(got) == 1 and "A_SENTINEL" in got[0]["content"]
    assert "仅为" in got[0]["content"], "应走选择器路径（带免责首句）"


def test_factory_wires_shared_set_from_loop(store, monkeypatch):
    """`_make_prefetch_factory` 必须从 loop 上取共享集合，而不是自己新建一个。

    这是真机缺陷的直接回归：factory 里若 `shared = None`（或新建 set），
    选择器与关键词注入器就各记一份，同一条记忆注入两遍。
    上面两个测试直接构造 `PrefetchState`，绕过了 factory，测不到这一层。
    """
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "描述 A")

    class FakeLoop:
        message_injector = None
        user_id = "u1"

    loop = FakeLoop()
    loop._memory_injected_slugs = {"already-there"}   # 关键词路径留下的标记
    loop._memory_injector = object()

    async def scenario():
        factory = mp._make_prefetch_factory(SelectorClient(["a"]), "u1")
        factory(loop, [{"role": "user", "content": "具体问题"}])
        return loop._memory_prefetch_state

    state = run(scenario())
    assert state.injected is loop._memory_injected_slugs, \
        "factory 必须复用 loop 上的共享集合，不能新建"
    assert "already-there" in state.injected, "上一路径写入的 slug 应可见"


def test_loop_without_factory_is_unchanged(store):
    """没装 factory 时行为与历史完全一致（记忆关掉不影响核心）。"""
    from app.agent_loop import AgentLoop
    from app.tools import ToolRegistry

    class Model:
        async def run(self, messages, tools=None, on_delta=None, stop_check=None, **kw):
            return ChatResult(content="hi")

    loop = AgentLoop(Model(), ToolRegistry([]), "sys", max_turns=3)
    assert loop.prefetch_factory is None

    async def scenario():
        history = [{"role": "user", "content": "x"}]
        return [e async for e in loop.run_stream(history)]

    evs = run(scenario())
    assert any(e.get("type") == "done" for e in evs)
    assert not any(e.get("type") == "message_injected" for e in evs)


def test_loop_cancels_unconsumed_prefetch(store, monkeypatch):
    """本轮没赶上消费点时，预取必须被取消 —— 否则成孤儿 task。"""
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR_MODEL", "small")
    _mem("a", "描述 A")

    from app.agent_loop import AgentLoop
    from app.tools import ToolRegistry

    class Model:
        async def run(self, messages, tools=None, on_delta=None, stop_check=None, **kw):
            return ChatResult(content="done")   # 一轮就结束，没有工具步骤

    loop = AgentLoop(Model(), ToolRegistry([]), "sys", max_turns=3, user_id="u1")
    slow = SelectorClient(["a"], delay=2.0)

    def factory(_loop, history):
        return mp.start_prefetch(_loop, history, client=slow, user_id="u1",
                                 state=mp.PrefetchState())
    loop.prefetch_factory = factory

    async def scenario():
        history = [{"role": "user", "content": "问一个具体问题"}]
        _ = [e async for e in loop.run_stream(history)]
        return loop._prefetch_handle

    handle = run(scenario())
    # 句柄被清空（_cancel_prefetch 置 None）
    assert handle is None
