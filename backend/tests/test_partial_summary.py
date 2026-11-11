"""上游彻底不可用时的收尾摘要。

真机 runId 131321-558a：三条任务全部 completed、三个 worker 都正常结束，
但 lead 在收尾阶段撞上上游持续 502，用户最后看到的是一句半截话。
劳动成果都在任务板和数据库里，却没有任何东西告诉用户。

关键约束：这段收尾**绝不能调模型** —— 它存在的场景就是模型服务已经不可用。
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, team_store  # noqa: E402
from app.partial_summary import build_partial_summary  # noqa: E402
from app.scope import TeamScope  # noqa: E402
from app.subagent import store as sub_store  # noqa: E402


def _ids() -> dict[str, str]:
    return {"user_id": "u_" + uuid.uuid4().hex[:8],
            "session_id": "s_" + uuid.uuid4().hex[:8],
            "generation_id": "g1",
            "team_id": "team_" + uuid.uuid4().hex[:6]}


def _lead_scope(ids: dict[str, str]) -> TeamScope:
    return TeamScope(user_id=ids["user_id"], session_id=ids["session_id"],
                     generation_id=ids["generation_id"], team_id=ids["team_id"],
                     agent_id="root", role="lead")


async def _finish(ids: dict[str, str], name: str, status: str, result: str = "") -> None:
    run_id = "run_" + uuid.uuid4().hex[:10]
    await sub_store.create_run(
        run_id=run_id, ctx_user_id=ids["user_id"], session_id=ids["session_id"],
        message_id="m1", parent_agent_id="root", agent_id=f"sub:{name}", name=name,
        role="teammate", task_message="t", status="running",
        generation_id=ids["generation_id"], team_id=ids["team_id"])
    await sub_store.finish_run(
        run_id, status=status, result_text=result, turns=3, error="",
        user_id=ids["user_id"], session_id=ids["session_id"],
        generation_id=ids["generation_id"], team_id=ids["team_id"])


@pytest.mark.asyncio
async def test_summary_lists_completed_and_unfinished():
    """核心行为：已完成的列出来，未完成的也标清楚。"""
    await db.init_db()
    await team_store.init_tables()
    ids = _ids()
    scope = _lead_scope(ids)

    t1 = await team_store.create_task(scope, subject="统计文件数", description="d")
    await team_store.bind_task_owner(scope, t1["task_id"], "sub:a",
                                     expected_version=t1["version"])
    worker = TeamScope(user_id=ids["user_id"], session_id=ids["session_id"],
                       generation_id=ids["generation_id"], team_id=ids["team_id"],
                       agent_id="sub:a", role="teammate")
    done = await team_store.complete_task(worker, t1["task_id"], expected_version=1)
    assert done["ok"], done
    await team_store.create_task(scope, subject="写总结", description="d")

    text = await build_partial_summary(**ids, user_text="统计并总结")

    assert "中断前已完成" in text
    assert "统计文件数" in text
    assert "尚未完成" in text and "写总结" in text
    assert "统计并总结" in text, "应复述用户请求，让用户知道是哪一次"
    assert "重发" in text, "应告诉用户下一步怎么做"

    await team_store.delete_session_data(ids["user_id"], ids["session_id"])


@pytest.mark.asyncio
async def test_summary_includes_teammate_results_not_on_board():
    """队友确实产出了结论、但任务板上没有对应行时，也要算进「已完成」。"""
    await db.init_db()
    await team_store.init_tables()
    ids = _ids()
    await _finish(ids, "digger", "completed", "统计结果：77 个 .py 文件")

    text = await build_partial_summary(**ids, user_text="数一下文件")

    assert "digger" in text, f"应提到队友，实际 {text!r}"
    assert "77 个 .py" in text, f"应带上它的结论，实际 {text!r}"

    await team_store.delete_session_data(ids["user_id"], ids["session_id"])


@pytest.mark.asyncio
async def test_summary_reports_failed_teammates():
    """失败/停止的队友要如实说明，不能假装都成功了。"""
    await db.init_db()
    await team_store.init_tables()
    ids = _ids()
    await _finish(ids, "worker_a", "errored")
    await _finish(ids, "worker_b", "completed", "算出来 232.45")

    text = await build_partial_summary(**ids, user_text="算平均值")

    assert "执行失败的队友" in text and "worker_a" in text
    assert "worker_b" in text, "成功的那位也要在摘要里"

    await team_store.delete_session_data(ids["user_id"], ids["session_id"])


@pytest.mark.asyncio
async def test_summary_is_empty_when_nothing_to_report():
    """什么都没做（比如第一次调用就挂了）时返回空串，保持原有报错行为。"""
    await db.init_db()
    await team_store.init_tables()
    ids = _ids()

    text = await build_partial_summary(**ids, user_text="随便问问")

    assert text == "", f"没有内容时不该硬凑，实际 {text!r}"


@pytest.mark.asyncio
async def test_summary_does_not_call_llm(monkeypatch):
    """核心约束：上游已经不可用，摘要不能依赖任何模型调用。"""
    await db.init_db()
    await team_store.init_tables()
    ids = _ids()
    await _finish(ids, "worker", "completed", "做完了")

    import app.llm_client as llm
    import app.llm as llm_mod

    def _boom(*a, **kw):
        raise AssertionError("摘要绝不该调用模型")

    monkeypatch.setattr(llm.LLMClient, "run", _boom, raising=False)
    monkeypatch.setattr(llm_mod, "build_client_from_config", _boom, raising=False)

    text = await build_partial_summary(**ids, user_text="干活")

    assert text, "不调模型也应该能生成摘要"


@pytest.mark.asyncio
async def test_summary_caps_item_count_and_truncates():
    """摘要要一眼能看完，不能把全部结论再抄一遍。"""
    await db.init_db()
    await team_store.init_tables()
    ids = _ids()
    scope = _lead_scope(ids)

    for i in range(20):
        await team_store.create_task(scope, subject=f"任务{i}", description="d")
    await _finish(ids, "worker", "completed", "很长的结论" * 500)

    text = await build_partial_summary(**ids, user_text="做很多事")

    assert len(text) < 4000, f"摘要过长（{len(text)} 字符），应受预算约束"
    assert "很长的结论" * 50 not in text, "单条结论要截断"

    await team_store.delete_session_data(ids["user_id"], ids["session_id"])

# ---------------- 落盘一致性：流里看得到，刷新后也必须还在 ----------------
@pytest.mark.asyncio
async def test_summary_text_reaches_persisted_content(monkeypatch):
    """真机缺陷（runId 134210-0e77）：收尾摘要只在 SSE 流里出现，刷新后消失。

    根因是 _persist 的 content 取自 buffer，而当时只往 blocks 里写、没写 buffer。
    这里用最小复现固定住「两处必须同步」这条不变量。
    """
    import inspect
    import re

    from app.routers import chat as chat_mod

    src = inspect.getsource(chat_mod._event_stream)
    # 找到收尾摘要那段，断言它同时写了 buffer 和 blocks
    seg_start = src.find("tail = await _partial_summary_blocks")
    assert seg_start >= 0, "收尾摘要没有接在 _event_stream 里"
    seg = src[seg_start:seg_start + 900]
    assert "buffer.append" in seg, "收尾文本必须写进 buffer，否则 _persist 的 content 里没有"
    assert "_append_text" in seg, "收尾文本必须写进 blocks，否则刷新后结构块里没有"


# ---------------- 端到端：error 事件触发摘要并落盘 ----------------
def _collect_frames(gen):
    import asyncio

    async def _run():
        out = []
        async for frame in gen:
            out.append(frame)
        return out
    return asyncio.run(_run())


def test_error_event_appends_summary_and_persists(monkeypatch):
    """端到端：模型调用彻底失败时，收尾摘要要进 SSE 流，也要进持久化内容。"""
    import asyncio
    import json as _json

    from app import db
    from app.routers import chat

    ids = _ids()
    captured: dict = {}

    async def fake_add_message(*a, **k):
        return {"id": k.get("message_id", "m")}

    async def fake_update_message(message_id, **fields):
        captured["content"] = fields.get("content")
        captured["blocks"] = fields.get("blocks")
        captured["status"] = fields.get("status")

    async def fake_run_agent_stream(ur, session_id, text, stop_event=None, attachments=None):
        yield {"type": "text_delta", "text": "我先看看"}
        yield {"type": "error", "error": "上游模型服务暂时不可用（502 Bad Gateway）"}

    class FakeModel:
        model_name = "fake"

    class FakeUR:
        model = FakeModel()

        async def sanitize_pending_tool_calls(self, sid):
            return False

    class FakeHub:
        async def get_user_runtime(self, uid, mid=""):
            return FakeUR()

    async def fake_summary(user_id, session_id, generation_id, turn, user_text):
        return ["\n\n", "> **执行中断说明**\n> 已完成 2 个任务。", "\n"]

    monkeypatch.setattr(chat, "get_hub", lambda: FakeHub())
    monkeypatch.setattr(chat, "run_agent_stream", fake_run_agent_stream)
    monkeypatch.setattr(chat, "_partial_summary_blocks", fake_summary)
    monkeypatch.setattr(db, "add_message", fake_add_message)
    monkeypatch.setattr(db, "update_message", fake_update_message)

    import app.compaction as compaction

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(compaction, "enforce_soft_limit", _noop, raising=False)
    monkeypatch.setattr(compaction, "enforce_hard_limit", _noop, raising=False)

    frames = _collect_frames(
        chat._event_stream(ids["user_id"], ids["session_id"], "干活", "g1", "m1"))

    assert "执行中断说明" in (captured.get("content") or ""), \
        f"收尾必须在持久化内容里，实际 {captured.get('content')!r}"
    text_blocks = [b for b in (captured.get("blocks") or []) if b.get("type") == "text"]
    assert any("执行中断说明" in b.get("text", "") for b in text_blocks), \
        "收尾也必须进 blocks，否则刷新后结构里没有"
    assert any("执行中断说明" in f for f in frames), "收尾也要流式发给用户"
    assert captured.get("status") == "error", "状态仍是 error，不能假装成功"


def test_complete_path_has_no_summary(monkeypatch):
    """正向对照：正常完成时绝不能塞中断说明。"""
    import asyncio

    from app import db
    from app.routers import chat

    captured: dict = {}

    async def fake_add_message(*a, **k):
        return {"id": "m"}

    async def fake_update_message(message_id, **fields):
        captured["content"] = fields.get("content")

    async def fake_run_agent_stream(ur, session_id, text, stop_event=None, attachments=None):
        yield {"type": "text_delta", "text": "全部搞定"}
        yield {"type": "done", "status": "complete"}

    class FakeModel:
        model_name = "fake"

    class FakeUR:
        model = FakeModel()

        async def sanitize_pending_tool_calls(self, sid):
            return False

    class FakeHub:
        async def get_user_runtime(self, uid, mid=""):
            return FakeUR()

    monkeypatch.setattr(chat, "get_hub", lambda: FakeHub())
    monkeypatch.setattr(chat, "run_agent_stream", fake_run_agent_stream)
    monkeypatch.setattr(db, "add_message", fake_add_message)
    monkeypatch.setattr(db, "update_message", fake_update_message)

    import app.compaction as compaction

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(compaction, "enforce_soft_limit", _noop, raising=False)
    monkeypatch.setattr(compaction, "enforce_hard_limit", _noop, raising=False)

    _collect_frames(chat._event_stream("u1", "s1", "hi", "g1", "m1"))

    assert "执行中断说明" not in (captured.get("content") or "")
    assert (captured.get("content") or "").strip() == "全部搞定"
