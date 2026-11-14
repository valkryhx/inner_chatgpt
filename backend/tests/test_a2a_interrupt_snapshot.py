"""A2A 中断现场：重启后不能只说「server restart」。

## 为什么这个测试值得存在

2026-11-14 实测：提交一个 90 秒的 A2A 任务，跑到一半杀掉后端重启，查到的是

    state=TASK_STATE_FAILED  err="server restart"  artifacts=[]

**任务进度全丢，且调用方看到的信息等于没有** —— 它不知道是跑到第几步断的、
已经生成了多少、重试会不会重复烧钱。这是「最糟的形态」：既没恢复，也没说清
发生了什么。

这组测试钉住「中断现场必须被记下来，并在恢复时如实说清」：

| 测试 | 钉什么 |
|---|---|
| `test_snapshot_persisted_during_run` | 跑的过程中进展被写进 DB（不是只在内存） |
| `test_snapshot_survives_without_any_extra_work` | 现场在**没有**优雅关闭的情况下也在（进程被 kill -9 也要有） |
| `test_restart_message_describes_progress` | 恢复后的 error 说明含步数 / 工具名 / 已生成字数 |
| `test_restart_message_without_snapshot_is_still_honest` | 没有现场时不许编造，要说「没有进展记录」 |
| `test_completed_task_not_touched_by_recovery` | 恢复只能动 SUBMITTED/WORKING，不能碰已完成 |
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
    from app.a2a import store

    path = tmp_path / "app.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setenv("INNER_A2A_ENABLED", "true")
    run(db.init_db())
    run(store.init_tables())

    async def seed():
        async with aiosqlite.connect(path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO users(id,email,username,password_hash,name,role,status,token_ver,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("u1", "u1@test.invalid", "u1", "x", "u1", "user", "active", 0, 0),
            )
            await conn.commit()

    run(seed())
    yield path


def _mk_task(task_id="task_x", state="TASK_STATE_WORKING"):
    from app.a2a import store

    return run(store.create_task(
        task_id=task_id, context_id="co_1", user_id="u1", agent_id="ha_1",
        session_id="s1", message_id="m1", generation_id="g1", state=state,
    ))


# ---------------------------------------------------------------- 现场写入

def test_snapshot_persisted_during_run(isolated_db):
    """进展写进 DB，不是只活在内存里。"""
    from app.a2a import store

    _mk_task()
    run(store.update_snapshot("task_x", steps=3, tools_run=2,
                              last_tool="code_run", partial_chars=512))
    row = run(store.get_task("u1", "ha_1", "task_x"))
    snap = json.loads(row["snapshot"])
    assert snap["steps"] == 3
    assert snap["tools_run"] == 2
    assert snap["last_tool"] == "code_run"
    assert snap["partial_chars"] == 512
    assert snap["at"] > 0


def test_snapshot_survives_process_kill(isolated_db):
    """模拟被 kill -9：写完现场后**不做任何优雅关闭**，直接读。

    现场必须在 DB 里 —— 这正是与「只在内存记状态」的根本区别。
    """
    from app.a2a import store

    _mk_task()
    run(store.update_snapshot("task_x", steps=7, tools_run=5,
                              last_tool="file_write", partial_chars=1024))
    # 不做任何 close/flush，换一个连接读
    async def read_fresh():
        async with aiosqlite.connect(isolated_db) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT snapshot FROM a2a_tasks WHERE task_id='task_x'")
            return (await cur.fetchone())["snapshot"]

    snap = json.loads(run(read_fresh()))
    assert snap["steps"] == 7
    assert snap["last_tool"] == "file_write"


# ---------------------------------------------------------------- 恢复说明

def test_restart_message_describes_progress(isolated_db):
    """恢复后的说明必须含步数 / 最后工具 / 已生成字数，而不是「server restart」。"""
    from app.a2a import store

    _mk_task()
    run(store.update_snapshot("task_x", steps=4, tools_run=3,
                              last_tool="code_run", partial_chars=321))
    n = run(store.mark_interrupted_tasks("server restart"))
    assert n >= 1

    row = run(store.get_task("u1", "ha_1", "task_x"))
    assert row["state"] == "TASK_STATE_FAILED"
    err = row["error"]
    # 必须能看出「跑到哪了」
    assert "4" in err and "步" in err, err
    assert "code_run" in err, err
    assert "321" in err, err
    # 必须明确不是正常的失败，而是被打断
    assert "中断" in err or "重启" in err, err
    # 不许再是那句没有信息量的原话
    assert err.strip() != "server restart"


def test_restart_message_without_snapshot_is_still_honest(isolated_db):
    """没有现场时不许编造进度 —— 如实说「没有进展记录」。"""
    import re

    from app.a2a import store

    _mk_task()
    run(store.mark_interrupted_tasks("server restart"))
    row = run(store.get_task("u1", "ha_1", "task_x"))
    err = row["error"]
    assert "没有进展记录" in err, err
    # 不能凭空出现「第 N 步」（N>=1）。注意不能简单地禁「步」字 ——
    # 「无法确定已执行到哪一步」里本来就有「步」，那是正当措辞。
    assert not re.search(r"第\s*[1-9]\d*\s*步", err), err
    # 也不能凭空出现「已生成 N 字」
    assert not re.search(r"已生成\s*[1-9]\d*\s*字", err), err


def test_recovery_only_touches_unfinished_tasks(isolated_db):
    """已完成 / 已取消的任务不能被恢复逻辑改写。"""
    from app.a2a import store

    _mk_task("task_done", state="TASK_STATE_COMPLETED")
    _mk_task("task_canceled", state="TASK_STATE_CANCELED")
    _mk_task("task_running", state="TASK_STATE_WORKING")
    run(store.update_snapshot("task_done", steps=2, tools_run=1,
                              last_tool="file_read", partial_chars=10))
    run(store.mark_interrupted_tasks("server restart"))

    done = run(store.get_task("u1", "ha_1", "task_done"))
    canceled = run(store.get_task("u1", "ha_1", "task_canceled"))
    running = run(store.get_task("u1", "ha_1", "task_running"))
    assert done["state"] == "TASK_STATE_COMPLETED"
    assert done["error"] == ""
    assert canceled["state"] == "TASK_STATE_CANCELED"
    assert running["state"] == "TASK_STATE_FAILED"
    assert running["error"] != ""


def test_snapshot_column_migrates_on_old_db(isolated_db, tmp_path):
    """老库（没有 snapshot 列）必须能平滑升级，且不丢已有行。"""
    from app.a2a import store

    # 手工造一个「老结构」的表
    async def make_old():
        async with aiosqlite.connect(isolated_db) as conn:
            await conn.executescript("""
                DROP TABLE IF EXISTS a2a_tasks;
                CREATE TABLE a2a_tasks (
                    task_id TEXT PRIMARY KEY, context_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    tool_policy TEXT NOT NULL DEFAULT 'full',
                    state TEXT NOT NULL, artifacts TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
            """)
            await conn.execute(
                "INSERT INTO a2a_tasks VALUES('task_old','co_1','u1','ha_1','s1','m1','g1',"
                "'full','TASK_STATE_COMPLETED','[]','',1,1)")
            await conn.commit()

    run(make_old())
    run(store.init_tables())
    row = run(store.get_task("u1", "ha_1", "task_old"))
    assert row is not None, "老行必须保留"
    assert row["state"] == "TASK_STATE_COMPLETED"
    assert json.loads(row["snapshot"]) == {}


# ---------------------------------------------------------------- 接线：_run_task 真的在写现场

def test_run_task_writes_snapshot_at_tool_boundaries(isolated_db, monkeypatch):
    """`_run_task` 必须在工具边界把进度写进 DB，而不是只在内存里记账。

    这条是「接线」测试：上面几条测的是 store 层的能力，这条测的是
    service 层**有没有真的用它**。缺了这条，「加了 update_snapshot 但
    忘了调用」这种错误测试抓不到。
    """
    from app.a2a import service
    from app.routers import chat
    from app.sse import sse_frame

    _mk_task("task_rt")

    async def stream(user_id, session_id, text_in, generation_id, message_id,
                     model_id="", stop_event=None, tool_policy="full"):
        yield sse_frame("message_start", {"message_id": message_id})
        yield sse_frame("tool_start", {"id": "t1", "name": "code_run"})
        yield sse_frame("tool_result", {"id": "t1", "name": "code_run", "is_error": False})
        yield sse_frame("tool_start", {"id": "t2", "name": "file_write"})
        yield sse_frame("tool_result", {"id": "t2", "name": "file_write", "is_error": False})
        yield sse_frame("text_delta", {"text": "done!"})
        # 故意不 yield done，然后抛异常 —— 模拟中途被打断
        raise RuntimeError("boom")

    monkeypatch.setattr(chat, "_event_stream", stream)
    monkeypatch.setattr(service.team_store, "acquire_generation",
                        lambda *a, **k: _true())
    monkeypatch.setattr(service, "reserve_generation", lambda *a, **k: None)
    monkeypatch.setattr(service, "release_generation_reservation", lambda *a, **k: None)

    row = run(store_get("task_rt"))
    run(service._run_task(row, "hi", ""))

    got = run(store_get("task_rt"))
    snap = json.loads(got["snapshot"])
    assert snap["steps"] == 2, snap          # 两次 tool_start
    assert snap["tools_run"] == 2, snap      # 两次成功 tool_result
    assert snap["last_tool"] == "file_write", snap
    assert snap["partial_chars"] == 5, snap  # "done!"
    assert got["state"] == "TASK_STATE_FAILED"


async def _true():
    return True


async def store_get(task_id):
    from app.a2a import store
    return await store.get_task("u1", "ha_1", task_id)


def test_failed_tool_not_counted_as_run(isolated_db, monkeypatch):
    """失败的工具不算「执行过」—— 但仍然是「最后试图执行的那个」。

    这个区别很重要：`tools_run` 用来判断「有多少副作用真的发生了」，
    失败的工具没有副作用。
    """
    from app.a2a import service
    from app.routers import chat
    from app.sse import sse_frame

    _mk_task("task_ft")

    async def stream(user_id, session_id, text_in, generation_id, message_id,
                     model_id="", stop_event=None, tool_policy="full"):
        yield sse_frame("tool_start", {"id": "t1", "name": "code_run"})
        yield sse_frame("tool_result", {"id": "t1", "name": "code_run", "is_error": True})
        raise RuntimeError("boom")

    monkeypatch.setattr(chat, "_event_stream", stream)
    monkeypatch.setattr(service.team_store, "acquire_generation",
                        lambda *a, **k: _true())
    monkeypatch.setattr(service, "reserve_generation", lambda *a, **k: None)
    monkeypatch.setattr(service, "release_generation_reservation", lambda *a, **k: None)

    row = run(store_get("task_ft"))
    run(service._run_task(row, "hi", ""))
    snap = json.loads(run(store_get("task_ft"))["snapshot"])
    assert snap["tools_run"] == 0, snap
    assert snap["last_tool"] == "code_run", snap
