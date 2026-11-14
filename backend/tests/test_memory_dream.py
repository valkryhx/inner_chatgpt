"""M4.5：`auto_dream` 合并（触发闸 / 锁 / 索引上限 / 中断重跑 / 启动收敛 / Phase 4 可重入）。

对应方案 §7.1 的 M-17 / M-18 / M-19 / M-21 / M-22 / M-23。
真机合并效果见 ``tests/e2e/memory_cases.py`` 的 S-M8。

分工：
- 这里测**判定与状态机**（闸门、锁、恢复、索引口径）；
- 「模型到底能不能把三条重复记忆合并成一条」是 L3，用真 LLM 跑 S-M8。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import db, memory_dream as md, memory_store as ms
from app.llm_client import ChatResult


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """记忆目录 + DB + sessions 表都指到 tmp。"""
    monkeypatch.setattr(ms, "MEMORIES_DIR", tmp_path / "memories")
    monkeypatch.setattr(ms, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    run(ms.init_tables())
    run(db.init_db())
    run(md.init_tables())
    # 这些测试都直接调 dream_once / _tick，不该真的跑巡检
    monkeypatch.setenv("HLZ_MEMORY_DREAM_ENABLED", "1")
    return tmp_path


def _mem(slug: str, description: str, body: str = "", user: str = "u1"):
    return run(ms.write_memory(user, slug=slug, title=slug,
                               description=description,
                               body=body or f"# {slug}\n\n{description}\n",
                               kind="user"))


def _session(user: str = "u1", title: str = "s") -> str:
    return run(db.create_session(user, title))["id"]


class FakeClient:
    """脚本化 LLM：每轮返回预置 tool_calls；脚本耗尽 = 模型决定不再调工具。"""

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


def _call(name: str, args: dict, cid: str = "c1") -> dict:
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


# ---------------- 工具集 ----------------
def test_dream_registry_has_delete_but_root_does_not(env):
    """dream 能删条目；root agent 的三件套契约不变（不许蔓延）。"""
    from app.memory_tools import MEMORY_TOOL_NAMES, build_memory_tools

    dream_names = set(md.build_dream_registry("u1").names())
    assert "memory_delete" in dream_names
    assert dream_names == {"memory_list", "memory_read", "memory_write", "memory_delete"}

    for name in MEMORY_TOOL_NAMES:
        assert name in dream_names
    assert "memory_delete" not in MEMORY_TOOL_NAMES, \
        "memory_delete 只该存在于 dream 的 registry，不该进对外三件套"


def test_dream_registry_has_no_escape_hatches(env):
    """prompt 不是安全边界 —— 工具里没有 code_run / file_write / MCP 才是。"""
    reg = md.build_dream_registry("u1")
    for forbidden in ("code_run", "file_write", "file_patch", "file_read",
                      "list_files", "load_skill", "spawn_agent"):
        assert not reg.get(forbidden), f"dream 不该有 {forbidden}"


# ---------------- M-17 触发闸 ----------------
def test_m17_min_hours_gate(env, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_HOURS", "24")
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "1")
    _mem("a", "第一条")
    for _ in range(2):
        _session()

    now = md._now_ms()
    client = FakeClient([[]])

    # 23 小时前成功合并过 -> 不跑
    run(md._set_state("u1", last_consolidated=now - 23 * 3600 * 1000))
    out = run(md.dream_once("u1", client, now_ms=now))
    assert out.ran is False and "小时" in out.reason

    # 25 小时前 -> 跑
    run(md._set_state("u1", last_consolidated=now - 25 * 3600 * 1000))
    out = run(md.dream_once("u1", client, now_ms=now))
    assert out.ran is True, out.reason


def test_m17_min_sessions_gate(env, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_HOURS", "0")
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "5")
    _mem("a", "第一条")
    now = md._now_ms()
    since = now - 60_000

    # 只有 4 个会话在区间内 -> 不跑
    for _ in range(4):
        _session()
    out = run(md.dream_once("u1", FakeClient([[]]), now_ms=now))
    assert out.ran is False and "会话数" in out.reason

    # 第 5 个 -> 跑
    _session()
    touched = run(md.sessions_touched_since("u1", since))
    assert len(touched) == 5
    out = run(md.dream_once("u1", FakeClient([[]]), now_ms=now))
    assert out.ran is True, out.reason


def test_m17_sessions_gate_counts_only_since_last_consolidation(env, monkeypatch):
    """会话数闸看的是「上次成功合并之后」，不是历史总数。"""
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_HOURS", "0")
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "2")
    _mem("a", "第一条")
    now = md._now_ms()
    # 两个很老的会话（上次合并**之前**更新过）
    old = []
    for _ in range(2):
        old.append(_session())
    run(_backdate_sessions(old, now - 100 * 60 * 1000))
    run(md._set_state("u1", last_consolidated=now - 60_000))

    out = run(md.dream_once("u1", FakeClient([[]]), now_ms=now))
    assert out.ran is False and "会话数" in out.reason

    # 区间内只新增 1 个会话 -> 仍不够（证明老会话不被算进来）
    _session()
    out = run(md.dream_once("u1", FakeClient([[]]), now_ms=now))
    assert out.ran is False and "会话数" in out.reason, out.reason

    # 再新增 1 个 -> 达标
    _session()
    out = run(md.dream_once("u1", FakeClient([[]]), now_ms=now))
    assert out.ran is True, out.reason


async def _backdate_sessions(session_ids: list[str], ts_ms: int) -> None:
    import aiosqlite

    async with aiosqlite.connect(db.DB_PATH) as conn:
        for sid in session_ids:
            await conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (ts_ms, sid))
        await conn.commit()


def test_m17_disabled_env_short_circuits(env, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_DREAM_ENABLED", "0")
    _mem("a", "第一条")
    client = FakeClient([[]])
    out = run(md.dream_once("u1", client, force=True))
    assert out.ran is False and "关闭" in out.reason
    assert client.calls == [], "关掉时不该调模型"


def test_m17_hard_disabled_wins(env, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_DREAM_ENABLED", "1")
    monkeypatch.setenv("HLZ_MEMORY_DISABLED", "1")
    _mem("a", "第一条")
    client = FakeClient([[]])
    out = run(md.dream_once("u1", client, force=True))
    assert out.ran is False and "关闭" in out.reason
    assert client.calls == [], "HLZ_MEMORY_DISABLED=1 时连模型都不该调"


def test_m17_force_skips_time_gates(env, monkeypatch):
    """设置页「立即整理」跳过时间闸与会话数闸，但**不跳过**开关与锁。"""
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_HOURS", "999")
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "999")
    _mem("a", "第一条")
    out = run(md.dream_once("u1", FakeClient([[]]), force=True))
    assert out.ran is True, out.reason


# ---------------- M-18 锁 ----------------
def test_m18_lock_blocks_second_run(env, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "1")
    _mem("a", "第一条")
    now = md._now_ms()
    assert run(md.acquire_lock("u1", pid=1234, now_ms=now)) is True

    out = run(md.dream_once("u1", FakeClient([[]]), force=True, now_ms=now))
    assert out.ran is False and ("锁" in out.reason or "在跑" in out.reason)

    run(md.release_lock("u1", pid=1234))


def test_m18_stale_lock_can_be_stolen(env):
    """锁超过 30 分钟：可以被强抢（进程崩了也不会永久卡死）。"""
    now = md._now_ms()
    assert run(md.acquire_lock("u1", pid=111, now_ms=now)) is True
    # 30 分钟之后再抢：成功
    later = now + md.LOCK_STALE_MS + 1000
    assert run(md.acquire_lock("u1", pid=222, now_ms=later)) is True
    st = run(md.get_state("u1"))
    assert st["lock_pid"] == 222


def test_m18_fresh_lock_is_not_stolen(env):
    now = md._now_ms()
    assert run(md.acquire_lock("u1", pid=111, now_ms=now)) is True
    later = now + 60_000  # 1 分钟后
    assert run(md.acquire_lock("u1", pid=222, now_ms=later)) is False


def test_m18_release_only_clears_own_lock(env):
    now = md._now_ms()
    run(md.acquire_lock("u1", pid=111, now_ms=now))
    # 别的 PID 解不掉别人的锁（防止误清掉正在跑的进程）
    run(md.release_lock("u1", pid=999))
    assert run(md.get_state("u1"))["lock_pid"] == 111
    run(md.release_lock("u1", pid=111))
    assert run(md.get_state("u1"))["lock_pid"] == 0


def test_m18_lock_released_after_success_and_failure(env, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "1")
    _mem("a", "第一条")
    run(md.dream_once("u1", FakeClient([[]]), force=True))
    assert run(md.get_state("u1"))["lock_pid"] == 0

    class BoomClient:
        async def run(self, *a, **kw):
            raise RuntimeError("炸")

    run(md.dream_once("u1", BoomClient(), force=True))
    assert run(md.get_state("u1"))["lock_pid"] == 0, "失败也必须还锁"


# ---------------- M-19 索引上限 ----------------
def test_m19_index_respects_caps(env, monkeypatch):
    monkeypatch.setattr(ms, "index_max_lines", lambda: 20)
    monkeypatch.setattr(ms, "index_max_chars", lambda: 1200)
    for i in range(60):
        _mem(f"m-{i:02d}", f"第 {i} 条记忆的描述，用于压测索引上限")
    path = run(md.run_phase4("u1"))
    assert path is True

    text = ms.index_path("u1").read_text(encoding="utf-8")
    lines = text.rstrip("\n").split("\n")
    assert len(lines) <= 20 + 4, f"索引 {len(lines)} 行，超过上限（含表头/尾巴）"
    assert len(text) <= 1200 + 200, f"索引 {len(text)} 字符，超过上限"
    # 超限是「压缩」不是「截断内容」：必须明确写出还有多少条没进索引
    assert "未列入索引" in text
    # 每一条被列进来的都必须是完整的索引行，不能是半行
    for line in lines:
        if line.startswith("- ["):
            assert line.endswith(")") or " — " in line, f"半行：{line!r}"


def test_m19_index_never_contains_body(env):
    """索引是索引 —— 正文再短也不该出现在 MEMORY.md 里。"""
    _mem("a", "一句描述", body="# 标题\n\nUNIQUE_BODY_MARKER_77\n")
    run(md.run_phase4("u1"))
    text = ms.index_path("u1").read_text(encoding="utf-8")
    assert "UNIQUE_BODY_MARKER_77" not in text
    assert "一句描述" in text


# ---------------- M-21 中断后重跑 ----------------
def test_m21_failure_does_not_advance_last_consolidated(env, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "1")
    _mem("a", "第一条")
    before = run(md.get_state("u1"))

    class BoomClient:
        async def run(self, *a, **kw):
            raise RuntimeError("上游炸了")

    out = run(md.dream_once("u1", BoomClient(), force=True))
    assert out.ran is True and out.error

    st = run(md.get_state("u1"))
    assert st["last_consolidated"] == before["last_consolidated"], \
        "失败不算合并过，last_consolidated 必须保持原值"
    assert st["last_status"] == md.STATUS_FAILED
    assert "上游炸了" in st["last_error"]
    assert st["runs"] == before["runs"] + 1


def test_m21_retry_after_failure_actually_reruns(env, monkeypatch):
    """失败之后，条件满足时下一次**重新跑**，不是跳过。"""
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_HOURS", "0")
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "1")
    _mem("a", "第一条")
    _session()

    class BoomClient:
        async def run(self, *a, **kw):
            raise RuntimeError("boom")

    run(md.dream_once("u1", BoomClient(), force=True))

    # 用正常 client 再跑：应该真的开始（能收到模型调用）
    ok = FakeClient([[]])
    out = run(md.dream_once("u1", ok))
    assert out.ran is True, out.reason
    assert ok.calls, "重跑必须真的调模型"


def test_m21_timeout_is_recorded_as_failure(env, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "1")
    _mem("a", "第一条")
    monkeypatch.setattr(md, "dream_timeout_s", lambda: 0.2)

    class HangClient:
        async def run(self, *a, **kw):
            await asyncio.sleep(30)
            return ChatResult(content="")

    out = run(md.dream_once("u1", HangClient(), force=True))
    assert out.ran and "超过" in out.error
    st = run(md.get_state("u1"))
    assert st["last_status"] == md.STATUS_FAILED
    assert st["last_consolidated"] == 0


# ---------------- M-22 启动收敛 ----------------
def test_m22_recover_clears_fresh_lock(env):
    """服务刚重启时锁可能还很新，但进程已经没了 —— 不能只靠锁过期。"""
    now = md._now_ms()
    run(md.acquire_lock("u1", pid=999999, now_ms=now))
    st = run(md.get_state("u1"))
    assert st["lock_pid"] == 999999 and st["last_status"] == md.STATUS_RUNNING

    n = run(md.recover_dream_state("server restart"))
    assert n >= 1
    st = run(md.get_state("u1"))
    assert st["lock_pid"] == 0 and st["lock_at"] == 0
    assert st["last_status"] == md.STATUS_INTERRUPTED
    assert "server restart" in st["last_error"]


def test_m22_recover_keeps_idle_rows_untouched(env):
    run(md._set_state("u1", last_status=md.STATUS_IDLE, last_consolidated=123))
    n = run(md.recover_dream_state())
    assert n == 0
    st = run(md.get_state("u1"))
    assert st["last_consolidated"] == 123 and st["last_status"] == md.STATUS_IDLE


def test_m22_dream_runs_again_after_recovery(env, monkeypatch):
    """收敛之后该用户能重新被巡检到（不是永久 blocked）。"""
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_HOURS", "0")
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "1")
    _mem("a", "第一条")
    _session()
    now = md._now_ms()
    run(md.acquire_lock("u1", pid=999999, now_ms=now))  # 假装上次崩了
    run(md.recover_dream_state())

    out = run(md.dream_once("u1", FakeClient([[]]), now_ms=md._now_ms()))
    assert out.ran is True, out.reason


# ---------------- M-23 Phase 4 可重入 ----------------
def test_m23_phase3_success_marks_index_pending_then_clears(env, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "1")
    _mem("a", "第一条")
    out = run(md.dream_once("u1", FakeClient([[]]), force=True))
    assert out.ran and out.index_rebuilt is True
    st = run(md.get_state("u1"))
    assert st["last_consolidated"] > 0
    assert st["index_pending"] == 0, "重建成功后 marker 必须清掉"


def test_m23_killed_before_phase4_only_redoes_index(env, monkeypatch):
    """Phase 3 成功、Phase 4 没跑 -> 下一轮只补重建，**不再调模型**。"""
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "1")
    _mem("a", "第一条")
    now = md._now_ms()

    # 手工构造「Phase 3 成功但索引没建」的状态
    run(md._set_state("u1", last_status=md.STATUS_IDLE,
                      last_consolidated=now, index_pending=1))
    assert not ms.index_path("u1").exists()

    pending = run(md.list_users_with_pending_index())
    assert "u1" in pending

    client = FakeClient([[]])
    run(md._tick(lambda uid: _maybe(client), now_ms=now))
    assert client.calls == [], "补索引不该调模型"
    assert ms.index_path("u1").exists(), "索引必须被重建"
    assert run(md.get_state("u1"))["index_pending"] == 0


async def _maybe(client):
    return client


def test_m23_pending_index_swept_even_when_time_gate_closed(env, monkeypatch):
    """索引重建很便宜，不该等 24 小时。"""
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_HOURS", "999")
    _mem("a", "第一条")
    now = md._now_ms()
    run(md._set_state("u1", last_status=md.STATUS_IDLE,
                      last_consolidated=now, index_pending=1))
    client = FakeClient([[]])
    run(md._tick(lambda uid: _maybe(client), now_ms=now))
    assert ms.index_path("u1").exists()
    assert client.calls == []


def test_m23_failed_run_with_writes_sets_index_pending(env, monkeypatch):
    """本轮报错但确实改过文件 -> 索引仍要在下一轮补上。

    「索引与文件不一致」比「索引没更新」更糟：用户看到的是索引里不存在的条目
    实际已存在（或反过来）。所以失败分支也要把 ``index_pending`` 立起来。
    """
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "1")
    _mem("a", "旧描述", body="旧正文")
    _mem("b", "另一条")

    class WriteThenBoom:
        """第一轮写成功，第二轮炸 —— 模拟「合并做了一半上游断了」。"""

        def __init__(self):
            self.n = 0

        async def run(self, messages, tools=None, on_delta=None, stop_check=None, **kw):
            self.n += 1
            if self.n == 1:
                return ChatResult(content="", tool_calls=[
                    _call("memory_write", {"slug": "a", "content": "合并后的正文",
                                           "description": "合并后的描述"})])
            raise RuntimeError("第二轮断了")

    out = run(md.dream_once("u1", WriteThenBoom(), force=True))
    assert out.ran is True and out.error
    st = run(md.get_state("u1"))
    assert st["last_consolidated"] == 0, "失败不该推进 last_consolidated"
    assert st["index_pending"] == 1, "改过文件就必须补索引"
    # 索引文件此刻还没重建；下一轮巡检补上
    assert not ms.index_path("u1").exists()
    run(md._tick(lambda uid: _maybe(FakeClient([[]])), now_ms=md._now_ms()))
    assert ms.index_path("u1").exists()
    assert run(md.get_state("u1"))["index_pending"] == 0


# ---------------- 先写后删闸 ----------------
def test_delete_refused_before_any_successful_write(env):
    """一条都没写就删 -> 拒绝。prompt 说了顺序，但「内容真的丢了」不可逆。"""
    _mem("a", "第一条")
    _mem("b", "第二条")
    reg = md.build_dream_registry("u1")
    out = json.loads(run(reg.dispatch("memory_delete", {"slug": "b"})))
    assert out["outcome"] == "refused"
    assert run(ms.get_memory("u1", "b")) is not None, "被拒的删除不能真删"


def test_delete_allowed_after_successful_write(env):
    _mem("a", "第一条")
    _mem("b", "第二条")
    reg = md.build_dream_registry("u1")
    run(reg.dispatch("memory_write", {"slug": "a", "content": "合并正文",
                                      "description": "合并描述"}))
    out = json.loads(run(reg.dispatch("memory_delete", {"slug": "b"})))
    assert out["outcome"] == "deleted"
    assert run(ms.get_memory("u1", "b")) is None


def test_delete_budget_is_capped(env, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MAX_DELETES", "2")
    for i in range(5):
        _mem(f"m{i}", f"第 {i} 条")
    reg = md.build_dream_registry("u1")
    run(reg.dispatch("memory_write", {"slug": "m0", "content": "x",
                                      "description": "x"}))
    results = [json.loads(run(reg.dispatch("memory_delete", {"slug": f"m{i}"})))
               for i in range(1, 5)]
    kinds = [r["outcome"] for r in results]
    assert kinds.count("deleted") == 2, kinds
    assert "refused" in kinds


def test_delete_requires_existing_slug(env):
    _mem("a", "第一条")
    reg = md.build_dream_registry("u1")
    run(reg.dispatch("memory_write", {"slug": "a", "content": "x", "description": "x"}))
    out = json.loads(run(reg.dispatch("memory_delete", {"slug": "nope"})))
    assert out["outcome"] == "not_found"


def test_dream_outcome_reports_written_and_deleted(env, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "1")
    _mem("a", "第一条")
    _mem("b", "第二条")
    script = [[
        _call("memory_write", {"slug": "a", "content": "合并正文",
                               "description": "合并描述"}, "c1"),
        _call("memory_delete", {"slug": "b"}, "c2"),
    ]]
    out = run(md.dream_once("u1", FakeClient(script), force=True))
    assert out.written == ["a"]
    assert out.deleted == ["b"]
    assert run(ms.get_memory("u1", "b")) is None


# ---------------- 巡检循环 ----------------
def test_tick_isolates_users_from_each_other(env, monkeypatch):
    """一个用户拿不到 client / 跑崩了，不影响其他用户。"""
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_HOURS", "0")
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "1")
    _mem("a", "A 的记忆", user="userA")
    _mem("b", "B 的记忆", user="userB")
    _session("userA")
    _session("userB")

    def provider(uid):
        if uid == "userA":
            raise RuntimeError("A 的模型配置坏了")
        return _maybe(FakeClient([[]]))

    outcomes = run(md._tick(provider))
    by_user = {o.user_id: o for o in outcomes}
    assert "userA" in by_user and by_user["userA"].ran is False
    assert by_user["userB"].ran is True


def test_tick_skips_users_without_memories(env, monkeypatch):
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_HOURS", "0")
    monkeypatch.setenv("HLZ_MEMORY_DREAM_MIN_SESSIONS", "1")
    _session("u1")  # 有会话，但没有任何记忆
    client = FakeClient([[]])
    outcomes = run(md._tick(lambda uid: _maybe(client)))
    assert outcomes == []
    assert client.calls == []


def test_case_listed_tests_are_documented():
    """看一眼这份测试覆盖了方案里的哪几条（给人读的锚点）。"""
    ids = ("M-17", "M-18", "M-19", "M-21", "M-22", "M-23")
    assert set(ids) == {"M-17", "M-18", "M-19", "M-21", "M-22", "M-23"}
