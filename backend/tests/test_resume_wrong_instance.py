"""续看端点必须区分「这一轮结束了」和「你连错实例了」（2026-11-19 审计 §4）。

## 问题

`resume_stream` 里 `job is None` 有两种完全不同的含义，原实现当成一种：

```
job = turn_job.get_live_job_for_session(user["id"], session_id)
if job is None:
    ...直接发 done...
```

1. **这一轮真的结束了** -> 发 `done` 是对的；
2. **这一轮在别的实例上跑着** -> 也发 `done`，于是客户端以为结束了。

第 2 种情况的后果（审计 §4 记录的「最隐蔽」一条）：发出去的 `done` 里
`status` 取自 DB，而那时它往往仍是 `'streaming'`；前端 `App.tsx:451` 的映射是

```ts
const status = ev.status === 'stopped' ? 'stopped'
            : ev.status === 'error'   ? 'error'
            : 'complete'
```

`'streaming'` 掉进 `else` -> **变成 `'complete'`**。于是「正在跑」显示成「已完成」，
并且 `setStreaming(false)` 之后不再重试。用户看到内容停在半截。

## 修法（与 409 路径用同一套判据）

`chat.py` 的 409 路径早就在用 `generation_owner.is_generation_dead_elsewhere`
判断「那个 owner 还活着吗」，只是续看端点没接上。这里接上：

- 有 active generation 且 owner 还活着 -> 返回 `409 wrong_instance`（显式失败）
- 否则 -> 照旧发 `done`（真的结束了）

## 为什么是 409 而不是 SSE 错误帧

这个端点在**还没开始流**的时候就知道答案了。用 HTTP 状态码表达，客户端可以
直接重试；塞一个 SSE error 帧进去反而要客户端解析两套东西。
与 429（订阅者过多）的处理方式一致。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import db, sqlcompat  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    mods = [db]
    for name in ("team_store", "turn_marker"):
        try:
            mods.append(__import__(f"app.{name}", fromlist=["DB_PATH"]))
        except Exception:  # pragma: no cover
            pass
    for m in mods:
        monkeypatch.setattr(m, "DB_PATH", path)
    run(db.init_db())
    yield path


async def _seed(path: Path, *, session_id="s1", user_id="u1",
                message_status="streaming") -> None:
    async with sqlcompat.connect(path) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users(id,email,name,username,password_hash,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, f"{user_id}@t.invalid", user_id, user_id, "x", db.now_ms()),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO sessions(id,user_id,title,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (session_id, user_id, "t", db.now_ms(), db.now_ms()),
        )
        await conn.execute(
            "INSERT INTO messages(id,session_id,role,content,created_at,status) "
            "VALUES(?,?,?,?,?,?)",
            (f"m_{session_id}", session_id, "assistant", "", db.now_ms(), message_status),
        )
        await conn.commit()


async def _set_gen_owner(path: Path, *, user_id="u1", session_id="s1",
                        generation_id="g1", owner="", status="active") -> None:
    from app import team_store
    await team_store.init_tables()
    async with sqlcompat.connect(path) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO session_generations"
            "(user_id,session_id,generation_id,status,started_at,owner) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, session_id, generation_id, status, db.now_ms(), owner),
        )
        await conn.commit()


# ============================================================ 核心：连错实例

def test_live_generation_elsewhere_is_wrong_instance(iso, monkeypatch):
    """owner 是别的实例且还活着 -> 必须返回 wrong_instance，不能假装 done。"""
    from app.routers import chat

    run(_seed(iso))

    async def fake_dead_elsewhere(*a, **kw):
        return False          # 还活着

    # 注意必须是**同步**函数：真实实现也是同步的（直接查 dict），
    # 写成 async 会让 _resume_decision 拿到一个永远 truthy 的协程对象，
    # 于是误判成 stream —— 这错误在第一次跑测试时就暴露了。
    monkeypatch.setattr(chat.turn_job, "get_live_job_for_session",
                        lambda *a, **kw: None)
    from app import generation_owner
    monkeypatch.setattr(generation_owner, "is_generation_dead_elsewhere",
                        fake_dead_elsewhere)

    from app import team_store
    monkeypatch.setattr(team_store, "get_active_generation",
                        lambda *a, **kw: _await("g1"))

    async def go():
        await _set_gen_owner(iso, owner="other-host:9")
        return await chat._resume_decision("u1", "s1")

    decision = run(go())
    assert decision["kind"] == "wrong_instance", \
        f"应当是 wrong_instance，实际 {decision}"


def test_finished_generation_still_sends_done(iso, monkeypatch):
    """没有 active generation -> 这一轮真结束了，照旧发 done。

    这是回归保护：修 §4 不能把「正常结束」也变成错误。
    """
    from app.routers import chat

    run(_seed(iso, message_status="complete"))

    monkeypatch.setattr(chat.turn_job, "get_live_job_for_session",
                        lambda *a, **kw: None)
    from app import team_store
    monkeypatch.setattr(team_store, "get_active_generation",
                        lambda *a, **kw: _await(None))

    decision = run(chat._resume_decision("u1", "s1"))
    assert decision["kind"] == "finished", f"应当是 finished，实际 {decision}"


def test_dead_owner_generation_sends_done(iso, monkeypatch):
    """owner 已死（不在存活集合） -> 当成结束处理，发 done。

    那个实例崩溃了，这一轮不可能再有事件。此时应当收尾而不是报错，
    否则用户刷新页面看到的是「连错实例」而不是「上一轮中断了」。
    """
    from app.routers import chat

    run(_seed(iso))

    async def fake_dead_elsewhere(*a, **kw):
        return True           # 确认死了

    monkeypatch.setattr(chat.turn_job, "get_live_job_for_session",
                        lambda *a, **kw: None)
    from app import generation_owner
    monkeypatch.setattr(generation_owner, "is_generation_dead_elsewhere",
                        fake_dead_elsewhere)
    from app import team_store
    monkeypatch.setattr(team_store, "get_active_generation",
                        lambda *a, **kw: _await("g1"))

    async def go():
        await _set_gen_owner(iso, owner="gone-host:1")
        return await chat._resume_decision("u1", "s1")

    decision = run(go())
    assert decision["kind"] == "finished", f"应当是 finished，实际 {decision}"


def test_local_job_present_is_stream(iso, monkeypatch):
    """本进程有这个 job -> 正常订阅（不受本次修改影响）。"""
    from app import turn_job
    from app.routers import chat

    run(_seed(iso))
    job = turn_job.TurnJob("u1", "s1", "g1", "m_s1")

    monkeypatch.setattr(chat.turn_job, "get_live_job_for_session",
                        lambda *a, **kw: job)

    decision = run(chat._resume_decision("u1", "s1"))
    assert decision["kind"] == "stream"
    assert decision["job"] is job


def test_wrong_instance_does_not_rely_on_status_field(iso, monkeypatch):
    """不能用「消息 status 还是 streaming」当判据。

    这正是原 bug 的思维：status 是**结果**，不是**原因**。别的实例上跑着时
    status 确实是 streaming，但「刚崩溃、status 也是 streaming」长得一样。
    只有 owner 存活判定能区分。
    """
    from app.routers import chat

    # status 说「已完成」，但集群说 owner 还活着 -> 仍应报 wrong_instance
    run(_seed(iso, message_status="complete"))

    async def fake_dead_elsewhere(*a, **kw):
        return False

    monkeypatch.setattr(chat.turn_job, "get_live_job_for_session",
                        lambda *a, **kw: None)
    from app import generation_owner
    monkeypatch.setattr(generation_owner, "is_generation_dead_elsewhere",
                        fake_dead_elsewhere)
    from app import team_store
    monkeypatch.setattr(team_store, "get_active_generation",
                        lambda *a, **kw: _await("g1"))

    async def go():
        await _set_gen_owner(iso, owner="other:5")
        return await chat._resume_decision("u1", "s1")

    decision = run(go())
    assert decision["kind"] == "wrong_instance", \
        "判据不能看 messages.status —— 它区分不了「别人在跑」和「刚崩溃」"


async def _await(value):
    return value


# ============================================================ owner 是自己

def test_own_generation_without_local_job_is_finished(iso, monkeypatch):
    """owner 是**我自己**但本进程没有这个 job -> 当成结束，不能报 wrong_instance。

    ## 这个 bug 是真机发现的（2026-11-19 多实例验证）

    三个实例全返回 `409 wrong_instance`，包括**拥有那个 generation 的实例自己**。
    原因是 `is_generation_dead_elsewhere` 对「owner 是我」刻意返回 False
    （它的语义是「别人那边死没死」，自己的情况交给调用方用进程内状态判断）——
    而 `_resume_decision` 把 False 一律解释成「别人还活着」。

    后果不只是报错文案不准：客户端会去换 cookie 重试，而重试还会落到同一个
    实例（那本来就是 owner），于是**重试永远无效**，用户看到死循环。

    ## 正确语义

    owner 是我 + 本进程没有 job = 这一轮在我这里已经结束/崩溃了。
    这也是最常见的情形：用户点了停止、release_generation 还没落库。
    """
    from app.routers import chat

    run(_seed(iso))

    monkeypatch.setattr(chat.turn_job, "get_live_job_for_session",
                        lambda *a, **kw: None)
    from app import generation_owner
    # 真实行为：owner 是自己时 is_generation_dead_elsewhere 返回 False
    monkeypatch.setattr(generation_owner, "is_generation_dead_elsewhere",
                        lambda *a, **kw: _await(False))
    from app import team_store
    monkeypatch.setattr(team_store, "get_active_generation",
                        lambda *a, **kw: _await("g1"))

    async def go():
        from app.sandbox.identity import owner_id
        await _set_gen_owner(iso, owner=owner_id())     # owner 就是我
        return await chat._resume_decision("u1", "s1")

    decision = run(go())
    assert decision["kind"] == "finished", \
        f"owner 是自己且无本地 job 时应当当成结束，实际 {decision}"


def test_other_generation_still_reports_wrong_instance(iso, monkeypatch):
    """对照：owner 是**别人**且活着 -> 仍然是 wrong_instance（不能因上一条改坏）。"""
    from app.routers import chat

    run(_seed(iso))

    monkeypatch.setattr(chat.turn_job, "get_live_job_for_session",
                        lambda *a, **kw: None)
    from app import generation_owner
    monkeypatch.setattr(generation_owner, "is_generation_dead_elsewhere",
                        lambda *a, **kw: _await(False))
    from app import team_store
    monkeypatch.setattr(team_store, "get_active_generation",
                        lambda *a, **kw: _await("g1"))

    async def go():
        await _set_gen_owner(iso, owner="other-host:9")
        return await chat._resume_decision("u1", "s1")

    assert run(go())["kind"] == "wrong_instance"