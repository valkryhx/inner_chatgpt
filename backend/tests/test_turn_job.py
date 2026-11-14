"""持久化第三步 · 阶段 1：生成与 HTTP 连接解耦。

## 为什么这组测试存在

实测确认（`docs/20261115-生成与连接解耦与断线续看-持久化第三步.md` §1）：
客户端断开 SSE 后，Starlette 的 `StreamingResponse` 取消整个 anyio task group，
而 `_event_stream` 这个生成器**就是生成本身** —— 于是刷新页面会掐死生成：

```text
请求 6000 字 → 断开后只产出 73 字，句子写在一半，status=stopped
```

阶段 1 把生成搬进独立的 `asyncio.Task`（turn job），SSE 只做订阅。
本文件钉住四件事：

| 测试 | 钉什么 |
|---|---|
| `test_job_survives_subscriber_detach` | 订阅者退出后 job 仍在跑 |
| `test_publish_reaches_all_subscribers` | 多订阅者都能收到同一帧 |
| `test_no_subscriber_does_not_block_generation` | 无人订阅时生成不阻塞 |
| `test_finish_notifies_and_removes` | 结束后通知订阅者并摘表 |
| `test_subscriber_limit` | 超上限拒绝 |
| `test_queue_overflow_closes_subscriber_only` | 溢出只断订阅者，不丢中间内容给别的订阅者 |
| `test_chat_guarded_stream_runs_generation_in_job` | **接线**：chat 真的用了 job |
| `test_register_generation_inside_job_task` | **接线**：登记的是 job 任务，不是请求任务 |
| `test_a2a_still_consumes_raw_event_stream` | A2A 仍能直接吃 `_event_stream` |
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app import turn_job


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ job 语义

def test_job_survives_subscriber_detach():
    """订阅者 detach 之后，job 的任务仍在跑、仍能 publish。

    这是整个阶段的目的：断开**不再**掐死生成。
    """
    async def main():
        seen: list[str] = []
        job = turn_job.TurnJob("u1", "s1", "g1")

        async def runner(j: turn_job.TurnJob) -> None:
            for i in range(3):
                await asyncio.sleep(0.01)
                j.publish(f"frame{i}")
                seen.append(f"frame{i}")

        sub = job.attach()
        turn_job.start_job(job, runner)
        # 立刻退订（模拟客户端断开）
        job.detach(sub)

        assert job.task is not None
        await asyncio.wait_for(job.task, timeout=5)
        # job 跑完了全部 3 帧 —— 而不是被 detach 打断
        assert seen == ["frame0", "frame1", "frame2"]
        assert job.finished is True

    run(main())


def test_publish_reaches_all_subscribers():
    """多个订阅者收到同一批帧（双标签页）。

    注意：阶段 2 起 publish 会**重写 `id:` 为 job 自己的 seq**（见
    `TurnJob._rewrite_seq`）。所以这里断言的是 data 内容，不是原始字符串。
    """
    async def main():
        job = turn_job.TurnJob("u1", "s1", "g1")
        a, b = job.attach(), job.attach()
        assert a is not None and b is not None

        job.publish("event: x\ndata: 1\nid: 1\n\n")
        job.publish("event: x\ndata: 2\nid: 2\n\n")
        job.finish()

        got_a = [f async for f in a.drain()]
        got_b = [f async for f in b.drain()]
        assert "data: 1" in got_a[0] and "data: 2" in got_a[1]
        assert got_a == got_b, "两个订阅者必须收到完全相同的帧"
        # seq 由 job 拥有：从 1 开始连续
        assert "id: 1" in got_a[0] and "id: 2" in got_a[1]

    run(main())


def test_no_subscriber_does_not_block_generation():
    """没有订阅者时 publish 直接丢弃，不得阻塞或抛错。

    用户关掉标签页后任务要继续跑完 —— 结果照样写库，只是没人实时看。
    """
    async def main():
        job = turn_job.TurnJob("u1", "s1", "g1")

        async def runner(j: turn_job.TurnJob) -> None:
            for i in range(5):
                j.publish(f"event: f\ndata: {i}\n\n")   # 没有订阅者，不能卡住
            await asyncio.sleep(0)

        turn_job.start_job(job, runner)
        await asyncio.wait_for(job.task, timeout=5)
        assert job.seq == 5
        assert job.finished is True
        assert len(job.replay) == 5, "无人订阅时也要进重放缓冲（供续看）"

    run(main())


def test_finish_notifies_and_removes():
    """finish 要通知所有订阅者「没有更多了」，并把 job 从表里摘掉。"""
    async def main():
        job = turn_job.TurnJob("u1", "s1", "g1")
        sub = job.attach()
        assert sub is not None

        async def runner(j: turn_job.TurnJob) -> None:
            j.publish("event: only\ndata: 1\n\n")
            await asyncio.sleep(0.01)

        turn_job.start_job(job, runner)
        await asyncio.wait_for(job.task, timeout=5)

        got = [f async for f in sub.drain()]
        assert len(got) == 1 and "data: 1" in got[0], "收到已发出的帧后干净结束，不挂住"
        # 直接查内部表，而不是 get_job() —— 后者自己会按 finished 过滤，
        # 于是「没摘表」也能通过（变异测试 M4 抓到的测试缺陷）。
        assert turn_job._key("u1", "s1", "g1") not in turn_job._JOBS, \
            "结束后必须从 _JOBS 真的摘掉，否则表会无限增长"
        assert turn_job.get_job("u1", "s1", "g1") is None

    run(main())


def test_subscriber_limit():
    """超过订阅者上限要返回 None，让调用方回错误帧而不是无限堆积。"""
    async def main():
        job = turn_job.TurnJob("u1", "s1", "g1")
        subs = [job.attach() for _ in range(turn_job.MAX_SUBSCRIBERS)]
        assert all(s is not None for s in subs)
        assert job.attach() is None

    run(main())


def test_queue_overflow_closes_subscriber_only(monkeypatch):
    """队列溢出只关掉这一个订阅者，且不丢别人的内容。

    构造要点：必须让「快订阅者」**边跑边取**。若两个都只发不取，两个都会溢出，
    那就测不到「只影响一个」这个性质了（第一版就是这样写错的）。
    """
    async def main():
        # 每帧重写后是 "event: x\ndata: 0123456789\nid: N\n\n" ≈ 38 字节，
        # 上限设 60 让「第二帧就超」成立，而 fast 每帧取走所以不会超。
        monkeypatch.setattr(turn_job, "_SUBSCRIBER_QUEUE_LIMIT_BYTES", 60)
        job = turn_job.TurnJob("u1", "s1", "g1")
        slow = job.attach()
        fast = job.attach()
        assert slow is not None and fast is not None

        fast_frames: list[str] = []
        fast_done = asyncio.Event()

        async def drain_fast():
            async for f in fast.drain():
                fast_frames.append(f)
            fast_done.set()

        drainer = asyncio.create_task(drain_fast())
        for i in range(5):
            job.publish(f"event: x\ndata: {i}\n\n")
            await asyncio.sleep(0)      # 让快订阅者有机会消费
        job.finish()
        await asyncio.wait_for(drainer, timeout=5)
        await asyncio.wait_for(fast_done.wait(), timeout=5)

        # slow 全程没取 → 溢出被关掉，但不能挂住
        slow_frames = [f async for f in slow.drain()]
        assert slow.overflow is True
        assert slow_frames == [], "溢出后不再投递"
        # fast 一直在取 → 拿到全部帧，溢出没有波及它
        assert len(fast_frames) == 5, f"快订阅者应拿到全部帧，实际 {fast_frames}"

    run(main())


# ------------------------------------------------------- 阶段 2：重放与 sync

def _job_with_frames(n: int, blocks=None):
    job = turn_job.TurnJob("u", "s", "g")
    if blocks is not None:
        job.set_blocks_provider(lambda: list(blocks))
    for i in range(1, n + 1):
        job.publish(f"event: text_delta\ndata: {{\"text\":\"t{i}\"}}\nid: {i}\n\n")
    return job


def test_seq_is_owned_by_job_and_contiguous():
    """job 重写每帧 id:，且序号从 1 连续递增 —— 续看游标依赖这个性质。

    `_event_stream` 自己的编号不连续（`healed` / 早退 error 走不带 id 的
    `sse_frame`），所以不能拿它当游标。
    """
    job = turn_job.TurnJob("u", "s", "g")
    job.publish("event: a\ndata: {}\n\n")                  # 没有 id 行
    job.publish("event: b\ndata: {}\nid: 999\n\n")         # id 是外部给的
    assert job.seq == 2
    assert [s for s, _ in job.replay] == [1, 2], "seq 必须连续"
    assert "id: 1" in job.replay[0][1]
    assert "id: 2" in job.replay[1][1]
    assert "id: 999" not in job.replay[1][1], "外部 id 必须被覆盖"


def test_replay_within_buffer():
    """after 落在缓冲覆盖范围内 → 纯增量重放，不发 sync。"""
    job = _job_with_frames(5)
    frames = job.sync_frames(3)
    assert len(frames) == 2, frames
    assert all("sync" not in f for f in frames), "窗口内不该发 sync"
    assert "t4" in frames[0] and "t5" in frames[1]


def test_replay_from_zero_is_incremental_when_buffer_complete():
    """缓冲完整时，after=0 也能纯增量 —— 不必要地发 sync 会白传一份快照。"""
    job = _job_with_frames(3)
    frames = job.sync_frames(0)
    assert len(frames) == 3
    assert all("event: sync" not in f for f in frames)


def test_replay_falls_back_to_sync_when_cursor_too_old():
    """游标早于缓冲最老 seq → 发 sync，用**此刻**状态重置。"""
    job = _job_with_frames(3, blocks=[{"type": "text", "text": "hello"}])
    # 人为把缓冲挤到只剩最后两帧
    job.replay.popleft()
    job.replay_bytes = sum(len(f) for _, f in job.replay)
    assert job.oldest_seq() == 2

    frames = job.sync_frames(0)          # 0 < 2-1 → 太老
    assert len(frames) == 1
    assert frames[0].startswith("event: sync"), frames[0]
    assert "hello" in frames[0], "sync 必须带上完整 blocks"
    assert f"id: {job.seq}" in frames[0]


def test_sync_uses_live_blocks_not_stale_snapshot():
    """sync 取的是**此刻** blocks（函数调用），不是过期快照。

    这是阶段 2 的核心正确性：`_persist` 有 250ms 节流，用落库快照会漏一段。

    ⚠️ 构造要点：必须**先逼出 sync 分支**（清空缓冲），**再**改 blocks，
    这样才有「调用时刻 vs 缓存时刻」的差别。第一版先取 sync 再改，读到的
    本来就都是 v1，变异 N4（缓存快照）照样通过 —— 测不到东西。
    """
    live = [{"type": "text", "text": "v1"}]
    job = turn_job.TurnJob("u", "s", "g")
    job.set_blocks_provider(lambda: list(live))

    job.publish("event: a\ndata: {}\n\n")
    job.publish("event: b\ndata: {}\n\n")
    job.replay.clear()                    # 先逼出 sync 分支
    job.replay_bytes = 0

    # 现在才改 blocks（模拟尚未落库的最新内容），再取 sync
    live[0]["text"] = "v2"
    frame = job.sync_frames(0)[0]
    assert frame.startswith("event: sync")
    assert "v2" in frame, "sync 必须读到此刻的 blocks，而不是注册时的快照"


def test_buffer_evicts_oldest_at_frame_limit(monkeypatch):
    """条数上限：超了就丢最老的，oldest_seq 跟着前进。"""
    monkeypatch.setattr(turn_job, "REPLAY_MAX_FRAMES", 3)
    job = _job_with_frames(6)
    assert len(job.replay) == 3
    assert job.oldest_seq() == 4
    assert job.seq == 6


def test_buffer_evicts_oldest_at_byte_limit(monkeypatch):
    """字节上限：条数没超但字节超了也要淘汰（几百条巨型工具结果）。"""
    monkeypatch.setattr(turn_job, "REPLAY_MAX_BYTES", 120)
    job = _job_with_frames(10)
    assert job.replay_bytes <= 120
    assert job.oldest_seq() > 1, "必须真的淘汰了"
    # 淘汰后仍能靠 sync 保证正确
    frames = job.sync_frames(0)
    assert frames and frames[0].startswith("event: sync")


def test_sync_after_equals_seq_returns_nothing():
    """客户端已经是最新（after == seq）→ 什么都不补，直接接实时。"""
    job = _job_with_frames(4)
    assert job.sync_frames(4) == []


def test_resume_endpoint_does_not_take_lease():
    """续看端点**不抢 generation 租约** —— 抢了就等于新开一轮。

    这是它与 `POST /messages` 的根本区别，必须钉住。
    """
    from app.routers import chat
    src = inspect.getsource(chat.resume_stream)
    assert "acquire_generation" not in src, \
        "续看不能抢租约（那会变成新开一轮）"
    assert "add_message" not in src, "续看不能产生新消息"
    assert "get_live_job_for_session" in src


def test_resume_endpoint_exists_with_after_param():
    """契约：`GET /sessions/{sid}/stream?after=N`。"""
    from app.routers import chat
    params = inspect.signature(chat.resume_stream).parameters
    assert "after" in params
    assert params["after"].default == 0


def test_chat_passes_job_into_event_stream():
    """接线：`_run_job` 必须把 job 传给 `_event_stream`，否则 blocks provider
    装不上，sync 永远是空快照。"""
    from app.routers import chat
    src = inspect.getsource(chat.send_message)
    assert "job=j" in src, "必须把 job 传进 _event_stream"
    esrc = inspect.getsource(chat._event_stream)
    assert "set_blocks_provider" in esrc, "_event_stream 必须注册 blocks provider"



    """**接线测试**：`guarded_stream` 必须用 turn_job，而不是直接跑生成。

    上面几条测的是 job 的能力；这条测 chat 有没有真的接上。
    缺了它，「实现了但没接线」抓不到。
    """
    from app.routers import chat

    src = inspect.getsource(chat.send_message)
    assert "guarded_stream" in src
    assert "turn_job.TurnJob" in src, "必须建 job"
    assert "start_job" in src, "必须启动 job"
    assert "sub.drain" in src, "SSE body 必须来自订阅队列"
    assert "job.detach" in src, "断开/结束要退订"


def test_register_generation_inside_job_task():
    """**关键**：`register_generation` 必须在 job 任务里调用。

    `cancel_generation` 读的是 `_generation_tasks[key]`，值来自
    `asyncio.current_task()`。如果登记成请求任务，请求一返回任务就 finished，
    `/stop` 就取消不到正在跑的生成 —— 停止按钮会变成没反应。
    """
    from app.routers import chat

    src = inspect.getsource(chat.send_message)
    assert "async def _run_job" in src, "生成必须包在 job runner 里"
    runner_body = src.split("async def _run_job", 1)[1]
    # ⚠️ 必须匹配「调用」而不是名字：unregister_generation 也含
    # register_generation 子串，第一版这样写导致变异 M2 直接漏过。
    assert "register_generation(user[" in runner_body, \
        "register_generation 必须在 job runner 内部调用"
    # 反向：runner 之外也不许有裸调用（否则登记的是请求任务）
    before = src.split("async def _run_job", 1)[0]
    assert "register_generation(user[" not in before, \
        "job 之外不得调用 register_generation —— 那会把请求任务登记成 live"


def test_registered_task_identity_is_job_task():
    """运行时验证：登记进 `_generation_tasks` 的必须是 job 任务本身。

    这条补上静态断言抓不到的情形 —— `register_generation` 被写在
    `_run_job` 正确的位置，但如果在 job 外先调用一次，`_generation_tasks`
    里留下的会是请求任务。
    """
    from app import runtime as rt

    async def main():
        job = turn_job.TurnJob("u_id", "s_id", "g_id")
        info: dict = {}

        async def runner(j: turn_job.TurnJob):
            info["job_task"] = asyncio.current_task()
            rt.register_generation("u_id", "s_id", "g_id")
            info["registered"] = rt._generation_tasks.get(
                rt._key("u_id", "s_id", "g_id"))
            try:
                await asyncio.sleep(30)
            finally:
                rt.unregister_generation("u_id", "s_id", "g_id")

        turn_job.start_job(job, runner)
        await asyncio.sleep(0.05)
        try:
            assert info["registered"] is info["job_task"], \
                "登记的任务必须是 job 自己的任务"
        finally:
            await turn_job.shutdown_all("test")

    run(main())


def test_a2a_still_consumes_raw_event_stream():
    """A2A 直接调用 `chat._event_stream`，解耦不得改变它的签名/产出语义。

    这是阶段 1 的硬约束：`a2a/service.py:193` 用 `async for raw in
    chat._event_stream(...)` 并自己 `_parse_frame`。若把 `_event_stream` 改成
    「建 job 返回订阅流」，A2A 会被套两层 job 且帧全部失配。
    """
    from app.a2a import service
    from app.routers import chat

    # A2A 侧仍然直接调 _event_stream
    a2a_src = inspect.getsource(service._run_task)
    assert "chat._event_stream" in a2a_src, "A2A 仍应消费原始事件流"

    # _event_stream 仍然是异步生成器，且签名没变
    assert inspect.isasyncgenfunction(chat._event_stream)
    params = list(inspect.signature(chat._event_stream).parameters)
    for name in ("user_id", "session_id", "text", "generation_id", "message_id"):
        assert name in params, f"签名缺少 {name}：{params}"


def test_shutdown_all_cancels_live_jobs():
    """进程退出时要收掉所有 job，不能留下跑不完的任务。"""
    async def main():
        job = turn_job.TurnJob("u9", "s9", "g9")

        async def runner(j: turn_job.TurnJob) -> None:
            await asyncio.sleep(30)   # 永不自然结束

        turn_job.start_job(job, runner)
        await asyncio.sleep(0.01)
        n = await turn_job.shutdown_all("test")
        assert n == 1
        assert turn_job.live_job_count() == 0

    run(main())
