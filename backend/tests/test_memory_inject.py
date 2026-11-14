"""M3：读侧注入（选择 / 预算 / 链式组合 / slug 去重 / 不持久化 / 预算单位）。

对应 ``docs/20261112-Hlaudz记忆系统实施方案.md`` §7.1 的
M-4 / M-5 / M-8 / M-10 / M-13 / M-14 / M-20。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import memory_store as ms
from app.memory_inject import (MemoryInjector, VOLATILE_KEY, combine,
                               strip_volatile, wrap_with_memory)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "MEMORIES_DIR", tmp_path / "memories")
    monkeypatch.setattr(ms, "DB_PATH", tmp_path / "app.db")
    run(ms.init_tables())
    return tmp_path


def _write(user="u1", slug="m", *, description="描述", kind="user", body="正文"):
    return run(ms.write_memory(user, slug=slug, title=slug, description=description,
                               kind=kind, body=body))


# ---------------- M-4 预算扣留 ----------------
def test_m4_budget_withholds_and_reports(store):
    for i in range(20):
        _write(slug=f"m{i:02d}", description=f"这是第 {i} 条记忆的描述文字")
    picked, withheld = run(ms.select_with_stats("u1", char_budget=500))
    assert 0 < len(picked) < 20
    assert withheld == 20 - len(picked)

    text = ms.format_memory_block(picked, withheld=withheld)
    assert text.startswith("[记忆]")
    assert f"另有 {withheld} 条记忆因预算未注入" in text
    # 预算（code point）确实没被突破太多：块头 + 每行
    assert len(text) <= 500 + 120


def test_m4_one_big_entry_does_not_block_small_ones(store):
    """一条超预算的长记忆不该让后面五条短记忆全部进不来。"""
    _write(slug="aaa-big", description="长" * 400)
    for i in range(5):
        _write(slug=f"zzz-small-{i}", description="短描述")
    picked = run(ms.select_for_injection("u1", char_budget=400))
    assert [m.slug for m in picked] == [] or all(m.slug != "aaa-big" for m in picked)
    assert len(picked) >= 3, "短记忆应该能塞进去"


# ---------------- M-5 优先级 ----------------
def test_m5_user_beats_project_when_budget_allows_one(store):
    _write(slug="p1", description="项目状态：正在做记忆功能", kind="project")
    _write(slug="u1", description="用户希望被叫老徐", kind="user")
    picked = run(ms.select_for_injection("u1", char_budget=60, max_entries=1))
    assert len(picked) == 1
    assert picked[0].kind == "user"


def test_m5_kind_priority_order_is_stable(store):
    for i, kind in enumerate(("project", "reference", "feedback", "user")):
        _write(slug=f"k{i}", description=f"{kind} 类描述", kind=kind)
    picked = run(ms.select_for_injection("u1"))
    assert [m.kind for m in picked] == ["user", "feedback", "reference", "project"]


def test_m5_keyword_hit_beats_kind_priority(store):
    _write(slug="a-user", description="用户希望被叫老徐", kind="user")
    _write(slug="b-proj", description="PDF 报告生成流程已经跑通", kind="project")
    picked = run(ms.select_for_injection("u1", query_hint="帮我看看 PDF 报告"))
    assert picked[0].slug == "b-proj"


def test_m5_tag_hit_contributes(store):
    run(ms.write_memory("u1", slug="tagged", title="t", description="无关描述",
                        kind="reference", tags=["部署", "看板"], body="x"))
    _write(slug="plain", description="无关描述二", kind="user")
    picked = run(ms.select_for_injection("u1", query_hint="部署在哪个看板"))
    assert picked[0].slug == "tagged"


# ---------------- M-8 链式组合 ----------------
def test_m8_compose_keeps_both_sync_injectors():
    def a():
        return [{"role": "user", "content": "A"}]

    def b():
        return [{"role": "user", "content": "B"}]

    got = run(combine((a, b))())
    assert [m["content"] for m in got] == ["A", "B"]


def test_m8_compose_supports_async_injector():
    """团队/子代理注入器都是协程；组合层拒绝 awaitable 会让它们静默消失。"""
    async def a():
        return [{"role": "user", "content": "A"}]

    def b():
        return [{"role": "user", "content": "B"}]

    got = run(combine((a, b))())
    assert [m["content"] for m in got] == ["A", "B"]


def test_m8_failing_injector_does_not_block_others():
    def boom():
        raise RuntimeError("炸了")

    def ok():
        return [{"role": "user", "content": "OK"}]

    got = run(combine((boom, ok))())
    assert [m["content"] for m in got] == ["OK"]


def test_m8_async_failure_isolated():
    async def boom():
        raise RuntimeError("炸了")

    def ok():
        return [{"role": "user", "content": "OK"}]

    assert [m["content"] for m in run(combine((boom, ok))())] == ["OK"]


def test_m8_wrap_puts_memory_first():
    def steer():
        return [{"role": "user", "content": "[用户在你执行过程中补充的消息] x"}]

    def memory():
        return [{"role": "user", "content": "[记忆] a"}]

    combined = wrap_with_memory(steer, memory)
    got = run(combined())
    assert got[0]["content"].startswith("[记忆]")
    assert got[1]["content"].startswith("[用户在你执行过程中补充的消息]")


def test_m8_wrap_with_none_returns_memory_fn():
    def memory():
        return [{"role": "user", "content": "[记忆] a"}]

    assert wrap_with_memory(None, memory) is memory


# ---------------- M-10 / M-14 / M-20 去重与不持久化 ----------------
def test_m20_only_new_slugs_injected_on_later_calls(store):
    _write(slug="aaa", description="第一条")
    injector = MemoryInjector("u1")

    first = run(injector())
    assert len(first) == 1
    assert "aaa" in first[0]["content"]

    # 第二轮没有新内容 -> 不注入（不是「重复注入 aaa」）
    assert run(injector()) == []

    # 第三轮多了一条 -> 只注入新的那条
    _write(slug="bbb", description="第二条")
    third = run(injector())
    assert len(third) == 1
    assert "bbb" in third[0]["content"]
    assert "aaa" not in third[0]["content"]


def test_m14_injected_block_is_volatile(store):
    _write(slug="aaa", description="第一条")
    got = run(MemoryInjector("u1")())
    assert got[0][VOLATILE_KEY] is True
    assert got[0]["content"].startswith("[记忆]")


def test_m10_history_store_strips_volatile(tmp_path, monkeypatch):
    """注入块不落 session_history（模型历史侧），但展示侧的 blocks 照旧。"""
    from app import history_store as hs

    monkeypatch.setattr(hs, "DB_PATH", tmp_path / "hist.db")
    store = hs.HistoryStore()
    run(store.init())

    history = [
        {"role": "user", "content": "你好"},
        {"role": "user", "content": "[记忆] x", VOLATILE_KEY: True},
        {"role": "assistant", "content": "在"},
    ]
    run(store.set("u1", "s1", history))
    persisted = run(store.get("u1", "s1"))
    assert [m["content"] for m in persisted] == ["你好", "在"]
    assert all(VOLATILE_KEY not in m for m in persisted)


def test_m10_append_path_also_strips(tmp_path, monkeypatch):
    from app import history_store as hs

    monkeypatch.setattr(hs, "DB_PATH", tmp_path / "hist2.db")
    store = hs.HistoryStore()
    run(store.init())
    run(store.append("u1", "s1", [{"role": "user", "content": "hi"}]))
    run(store.append("u1", "s1", [
        {"role": "user", "content": "[记忆] y", VOLATILE_KEY: True},
        {"role": "assistant", "content": "ok"},
    ]))
    persisted = run(store.get("u1", "s1"))
    assert [m["content"] for m in persisted] == ["hi", "ok"]


def test_strip_volatile_also_drops_bare_marker():
    out = strip_volatile([{"role": "user", "content": "x", VOLATILE_KEY: False}])
    assert out == [{"role": "user", "content": "x"}]


# ---------------- M-13 预算单位 ----------------
def test_m13_index_budget_is_code_points_not_bytes(store, monkeypatch):
    """25,000 个汉字（≈75KB UTF-8）不触发截断；按字节实现会截在约 8,300 字。"""
    monkeypatch.setattr(ms, "index_max_chars", lambda: 25000)
    monkeypatch.setattr(ms, "index_max_lines", lambda: 200)
    # 每条描述 500 个汉字，20 条 = 10,000 字（远低于 25,000 code unit，却已超过 25KB 字节）
    for i in range(20):
        _write(slug=f"m{i:02d}", description="记" * 500)
    target = run(ms.rebuild_index("u1"))
    text = target.read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) > 25000, "构造前提：字节数已超 25K"
    assert "未列入索引" not in text, "按 code point 计不该截断"


def test_m13_index_truncates_when_over_cap(store, monkeypatch):
    monkeypatch.setattr(ms, "index_max_chars", lambda: 2000)
    monkeypatch.setattr(ms, "index_max_lines", lambda: 200)
    for i in range(40):
        _write(slug=f"m{i:02d}", description="记" * 300)
    target = run(ms.rebuild_index("u1"))
    lines = target.read_text(encoding="utf-8").splitlines()
    assert "未列入索引" in "\n".join(lines), "超限时必须有欠账说明"
    assert len(target.read_text(encoding="utf-8")) < 2000 + 200


def test_m13_recall_limit_is_real_bytes(store, monkeypatch):
    """单文件召回按真字节（4096），与索引口径不同。"""
    monkeypatch.setattr(ms, "recall_max_bytes", lambda: 100)
    _write(slug="cjk", description="中文", body="中" * 200)
    from app.memory_tools import build_memory_tools
    tools = {t.name: t for t in build_memory_tools("u1")}
    out = json.loads(run(tools["memory_read"].call({"slug": "cjk"})))
    assert out["truncated"] is True
    assert len(out["content"].encode("utf-8")) <= 100


# ---------------- 其它不变量 ----------------
def test_query_hint_can_be_updated_mid_generation(store):
    _write(slug="aaa", description="关于 PDF 的")
    _write(slug="bbb", description="关于速度的")
    inj = MemoryInjector("u1")
    inj.set_query_hint("PDF")
    got = run(inj())
    assert "aaa" in got[0]["content"]


def test_injector_disabled_by_env(store, monkeypatch):
    _write(slug="aaa", description="第一条")
    monkeypatch.setenv("HLZ_MEMORY_ENABLED", "0")
    assert run(MemoryInjector("u1")()) == []


def test_injector_failure_returns_empty(store, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("炸了")

    monkeypatch.setattr(ms, "select_with_stats", boom)
    assert run(MemoryInjector("u1")()) == []


def test_injector_is_user_scoped(store):
    _write("userA", "a", description="A 的记忆")
    _write("userB", "b", description="B 的记忆")
    got_a = run(MemoryInjector("userA")())
    got_b = run(MemoryInjector("userB")())
    assert "A 的记忆" in got_a[0]["content"] and "B 的记忆" not in got_a[0]["content"]
    assert "B 的记忆" in got_b[0]["content"] and "A 的记忆" not in got_b[0]["content"]


def test_install_wraps_existing_injector(store):
    from app.memory_inject import install_memory_injector

    class FakeLoop:
        message_injector = None
        _steer_aware = False

    loop = FakeLoop()

    def steer():
        return [{"role": "user", "content": "[用户在你执行过程中补充的消息] hi"}]

    loop.message_injector = steer
    _write(slug="aaa", description="第一条")
    inj = install_memory_injector(loop, "u1")
    assert inj is not None
    assert loop.message_injector is not steer
    got = run(loop.message_injector())
    assert got[0]["content"].startswith("[记忆]")
    assert got[1]["content"].startswith("[用户在你执行过程中补充的消息]")


def test_install_does_not_skip_on_steer_aware(store):
    """``_steer_aware`` 的语义是「已有人在 drain 插话」，不是「不许装注入器」。"""
    from app.memory_inject import install_memory_injector

    class FakeLoop:
        message_injector = None
        _steer_aware = True

    _write(slug="aaa", description="第一条")
    loop = FakeLoop()
    assert install_memory_injector(loop, "u1") is not None
    assert run(loop.message_injector())[0]["content"].startswith("[记忆]")


def test_reinstall_does_not_accumulate_wrappers(store, monkeypatch):
    """``loop`` 跨轮复用：重复 install 不能让包装链增长。

    ``RuntimeHub`` 按 ``(user, model)`` 缓存 ``UserRuntime``，普通路径拿到的
    就是缓存里那个 loop。每轮套一层会让跑 100 轮后套 100 层，而**每一层都会
    做一次完整的记忆查询 + 排序** —— 每次模型调用 O(N) 次 DB 查询。

    用「一次注入过程中
    ``select_with_stats`` 被调用了几次」来量：正确实现恒为 1，叠加实现等于层数。
    """
    from app.memory_inject import install_memory_injector

    class FakeLoop:
        message_injector = None
        _steer_aware = False

    _write(slug="aaa", description="第一条")

    calls = {"n": 0}
    real_select = ms.select_with_stats

    async def counting_select(*a, **kw):
        calls["n"] += 1
        return await real_select(*a, **kw)

    monkeypatch.setattr(ms, "select_with_stats", counting_select)

    loop = FakeLoop()
    for _ in range(20):
        install_memory_injector(loop, "u1")

    run(loop.message_injector())
    assert calls["n"] == 1, f"包装链增长了：一次注入做了 {calls['n']} 次记忆查询"


def test_each_generation_reinjects_same_memory(store):
    """每一代都是独立上下文，同一批记忆要在新一代重新注入。

    这条锁住「``_injected_slugs`` 的生命周期是 per-generation」：
    如果注入器实例被跨代复用，第二代就什么都不会注入了。
    """
    from app.memory_inject import install_memory_injector

    class FakeLoop:
        message_injector = None
        _steer_aware = False

    _write(slug="aaa", description="第一条")
    loop = FakeLoop()

    install_memory_injector(loop, "u1")
    first = run(loop.message_injector())
    assert any("aaa" in (m.get("content") or "") for m in first)

    install_memory_injector(loop, "u1")
    second = run(loop.message_injector())
    assert any("aaa" in (m.get("content") or "") for m in second), \
        "新一代必须重新注入同一批记忆"
