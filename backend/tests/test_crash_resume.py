"""续跑（持久化第二步）：崩溃中断后不能盲目重放工具。

## 为什么这组测试存在

`agent_loop.sanitize()` 修复悬空 tool_call 的方式有两种：

1. 把不完整的 assistant+tool 块**删掉**；
2. 如果缺结果的是**尾部**且工具名还能解析，就**真的重新 dispatch 一次**
   （`agent_loop.py:270-281`：`out = await self.registry.dispatch(name, args)`）。

第 2 种在「用户主动停止」时是合理的 —— 停止那一刻工具多半还没跑完，
重放相当于替用户把这一步补上。

但**进程崩溃**时它很危险：那个 `code_run` 可能已经执行了（写了一半文件、
发了一条消息、调了一次外部接口），重放就是重复副作用。而崩溃后我们
**无法确定**它到底执行了没有。

所以正确的做法不是「假装知道」，而是：

- 把悬空的那一条补成**明确的「执行状态未知」结果**，让模型据此决策；
- 绝不静默重放。

这组测试钉住这个区别。

| 测试 | 钉什么 |
|---|---|
| `test_crash_marker_not_replayed` | 崩溃标记存在时，不重放 |
| `test_user_stop_still_replays` | 用户停止（无崩溃标记）时，行为不变 |
| `test_crash_marker_is_explicit` | 补的结果明确说「未知」，不伪装成功/失败 |
| `test_marker_survives_restart` | 标记走 DB，进程重启后仍生效 |
"""
from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest

from app import db


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    from app import turn_marker

    path = tmp_path / "app.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(turn_marker, "DB_PATH", path)
    run(db.init_db())
    run(turn_marker.init_tables())
    yield path


def _make_loop(dispatch_calls: list):
    """造一个最小 AgentLoop：registry 会记账，不真的执行。"""
    from app.agent_loop import AgentLoop
    from app.tools import Tool, ToolRegistry

    async def call(args):
        dispatch_calls.append(args)
        return "EXECUTED"

    registry = ToolRegistry([
        Tool("code_run", "", {"type": "object", "properties": {}}, call),
        Tool("file_write", "", {"type": "object", "properties": {}}, call),
    ])

    class FakeClient:
        pass

    return AgentLoop(FakeClient(), registry, "SYS", history_store=None)


def _dangling_history():
    """一条「AI 发了 tool_calls 但没有任何 tool 结果」的尾部历史。"""
    return [
        {"role": "user", "content": "跑个脚本"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "code_run", "arguments": "{\"code\":\"print(1)\"}"}}
        ]},
    ]


# ---------------------------------------------------------------- 现状基线

def test_user_stop_still_replays(isolated_db):
    """基线：**没有**崩溃标记时，尾部悬空仍然重放（既有行为，不能变）。

    这是「用户点停止后又发消息」的路径。重放是诚实的：那一轮是用户
    主动打断的，工具大概率没跑。
    """
    calls: list = []
    loop = _make_loop(calls)
    history = _dangling_history()

    changed = run(loop.sanitize(history))

    assert changed is True
    assert calls, "没有崩溃标记时应当照旧重放"
    assert any(m.get("role") == "tool" for m in history)


# ---------------------------------------------------------------- 崩溃场景

def test_crash_marker_not_replayed(isolated_db):
    """有崩溃标记时**绝不重放** —— 那个工具可能已经执行过。"""
    from app import turn_marker

    calls: list = []
    loop = _make_loop(calls)
    history = _dangling_history()

    run(turn_marker.mark_crashed("s1", last_tool="code_run", steps=3))
    crash = run(turn_marker.get_crash_marker("s1"))
    loop.set_crash_context(crash)

    changed = run(loop.sanitize(history))

    assert changed is True, "仍应修复历史（补一条结果或删块）"
    assert calls == [], f"崩溃场景下不许重放，但 dispatch 被调了：{calls}"
    # 必须有一条明确的「未知」结果让模型看见
    tool_msgs = [m for m in history if m.get("role") == "tool"]
    assert len(tool_msgs) == 1, history
    assert "未知" in tool_msgs[0]["content"], tool_msgs[0]


def test_crash_marker_is_explicit(isolated_db):
    """补的结果必须明确说「执行状态未知」，不能伪装成成功或普通失败。"""
    from app import turn_marker

    calls: list = []
    loop = _make_loop(calls)
    history = _dangling_history()
    run(turn_marker.mark_crashed("s2", last_tool="code_run", steps=2))
    loop.set_crash_context(run(turn_marker.get_crash_marker("s2")))

    run(loop.sanitize(history))

    content = [m for m in history if m.get("role") == "tool"][0]["content"]
    # 要说清三件事：没执行完 / 状态未知 / 先核对再重试
    assert "未知" in content
    assert "code_run" in content or "核对" in content
    # 不能伪装成成功
    assert "EXECUTED" not in content


def test_marker_survives_restart(isolated_db):
    """标记必须落库 —— 进程重启后仍然生效。"""
    from app import turn_marker

    run(turn_marker.mark_crashed("s3", last_tool="file_write", steps=5))

    async def read_fresh():
        async with aiosqlite.connect(isolated_db) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT * FROM turn_crash_markers WHERE session_id='s3'")
            return await cur.fetchone()

    row = run(read_fresh())
    assert row is not None
    assert row["last_tool"] == "file_write"
    assert row["steps"] == 5


def test_marker_cleared_after_successful_turn(isolated_db):
    """正常跑完一轮后要清掉标记，否则下次用户停止也会被当成崩溃。"""
    from app import turn_marker

    run(turn_marker.mark_crashed("s4", last_tool="code_run", steps=1))
    run(turn_marker.clear_crash_marker("s4"))
    assert run(turn_marker.get_crash_marker("s4")) is None


def test_no_marker_means_no_crash_context(isolated_db):
    """没有标记时 `set_crash_context(None)` 应保持原有重放行为。"""
    calls: list = []
    loop = _make_loop(calls)
    history = _dangling_history()
    loop.set_crash_context(None)
    run(loop.sanitize(history))
    assert calls, "无标记时不该改变既有行为"


# ---------------------------------------------------------------- 接线：chat 层真的用了它

def test_chat_injects_crash_context_before_sanitize(isolated_db):
    """`chat._event_stream` 必须先查崩溃标记再 sanitize。

    这条是接线测试：上面几条测 `AgentLoop.set_crash_context` 的能力，
    这条测**调用方有没有接上**。缺了它，「实现了但没接线」抓不到。
    """
    import inspect as _inspect

    from app.routers import chat

    src = _inspect.getsource(chat._event_stream)
    assert "get_crash_marker" in src, "chat 必须查崩溃标记"
    assert "set_crash_context" in src, "chat 必须把标记注入 loop"
    # 顺序：查标记必须在 sanitize 之前（否则已经被重放了）
    assert src.index("get_crash_marker") < src.index("sanitize_pending_tool_calls"), \
        "查标记必须在 sanitize 之前"


def test_chat_clears_marker_on_normal_exit(isolated_db):
    """正常结束要清标记，否则下次用户停止会被误当成崩溃。"""
    import inspect as _inspect

    from app.routers import chat

    src = _inspect.getsource(chat._event_stream)
    assert "clear_crash_marker" in src, "正常出口必须清标记"


def test_startup_marks_crashed_before_converging(isolated_db):
    """启动收敛必须**先标记、后收敛** —— 顺序反了证据就没了。

    这条测试最早写的是「main.py 里要调 mark_all_unfinished_crashed」，
    结果真机验证发现标记永远是 0：因为 `db.init_db()` **内部**就把
    streaming 收敛成 stopped 了，标记跑在它后面看不到证据。

    修法是把标记挪进 `init_db` 内部、夹在那条 UPDATE 之前。所以这里
    改为断言源码里的**顺序**，而不是断言某个函数被调用。
    """
    import inspect as _inspect

    from app import db

    src = _inspect.getsource(db.init_db)
    assert "_mark_crashed_before_converge" in src, "init_db 必须先建标记"
    assert "status='stopped'" in src, "init_db 仍要收敛"
    assert src.index("_mark_crashed_before_converge") < src.index("status='stopped'"), \
        "建标记必须在收敛之前，否则证据被抹掉"


def test_mark_then_converge_end_to_end(isolated_db):
    """端到端：一条 streaming 消息，跑一次 init_db 后应留下标记。"""
    from app import turn_marker

    run(db.add_message("s_crash", "user", content="hi"))
    run(db.add_message("s_crash", "assistant", content="", status="streaming",
                       message_id="m_crash"))
    # init_db 会先标记再收敛
    run(db.init_db())

    assert run(turn_marker.get_crash_marker("s_crash")) is not None, \
        "崩溃证据必须被保留成标记"
    msgs = run(db.list_messages("s_crash"))
    asst = [m for m in msgs if m["role"] == "assistant"]
    assert asst and asst[0]["status"] == "stopped", "消息本身仍要收敛"


def test_mark_all_unfinished_detects_streaming(isolated_db):
    """有 streaming 消息的会话才会被标；已 stopped 的不算。"""
    from app import turn_marker

    run(db.add_message("s_live", "user", content="hi"))
    run(db.add_message("s_live", "assistant", content="", status="streaming",
                       message_id="m_live"))
    run(db.add_message("s_done", "user", content="hi"))
    run(db.add_message("s_done", "assistant", content="ok", status="complete",
                       message_id="m_done"))

    n = run(turn_marker.mark_all_unfinished_crashed("restart"))
    assert n == 1
    assert run(turn_marker.get_crash_marker("s_live")) is not None
    assert run(turn_marker.get_crash_marker("s_done")) is None
