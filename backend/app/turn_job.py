"""Turn job：把「生成」与「HTTP 连接」解耦。

## 为什么需要这个模块

`StreamingResponse` 在 ASGI spec < 2.4 下把「发送 body」和「监听断开」放进同一个
anyio task group（Starlette 0.50 的 `StreamingResponse.__call__`）。客户端一断开，
整组被取消 —— 而 `_event_stream` 这个生成器**就是生成本身**，于是生成被掐死、
消息被收敛成 `stopped`。

实测（`docs/20261115-生成与连接解耦与断线续看-持久化第三步.md` §1）：请求 6000 字，
断开后只产出 73 字且句子写在一半，且库里状态与「用户主动停止」完全一致。

## 所以

生成跑在一个独立的 `asyncio.Task`（本模块的 job）里，SSE 只是**订阅者**。
订阅者断开只影响「看」，不影响生成；已产出的内容照常落库（`_persist` 在
`_event_stream` 内部，与订阅者无关）。

## 本模块的阶段范围

持久化第三步的**阶段 1：解耦**；**阶段 2：有界缓冲 + sync 兜底**。

阶段 2 的目标是「刷新后能接着看」。做法不是落一张事件日志表（`text_delta` 是 token 级
事件，一轮几千条 INSERT，而 `blocks` 250ms 才写一次 —— 见设计文档 §3.1），而是：

> **续看 = 用 `blocks` 快照对齐到某个 seq，再从该 seq 之后接上缓冲里的增量。**

缓冲只覆盖「还在 streaming」这个短窗口，轮次结束即随 job 一起消失。真相始终是
`messages.blocks`。这个构造有个好性质：**缓冲溢出/对不上时降级为「整份快照重发」，
永远正确**，因为 `blocks` 是权威且单调的。

### seq 的所有权

**job 拥有 seq**，并重写每帧的 `id:` 行。原因：`_event_stream` 自己的编号不连续
（`healed`、早退 error 走的是不带 id 的 `sse_frame`），而客户端续看游标必须单调、
无洞、且与缓冲键一致。所以 `publish()` 把任何帧的 `id:` 换成自己的 seq。

### 为什么不存「已落库的快照」而用实时 blocks 引用

最初设计是让 `_event_stream` 每次 `_persist` 后把快照交给 job，记 `blocks_seq`，
sync 时用那份快照 + 从 `blocks_seq` 补增量。核对后发现它有个必然会漏一段的窗口：

`_persist` 有 **250ms 节流**，所以落库快照总是落后于 `blocks` 的当前状态。若缓冲
恰好在这期间把 `blocks_seq` 之后的帧挤掉了，sync 快照与缓冲之间就有一个填不上的洞
（设计文档 §8.0 ② 记的就是这个）。

改成**直接持有 `blocks` 列表的引用**（`_event_stream` 就地 append 的那个对象）后，
sync 时序列化的是「此刻的全部状态」，`seq` 也是此刻的值 —— **不存在落后，也就不存在洞**。
代价是需要一次浅拷贝 `[dict(b) for b in blocks]`；因为 `json.dumps` 在事件循环里
同步跑完，不存在「序列化到一半被别的协程改了」的情况。

于是缓冲的淘汰也自由了：任何 `after` 太老的客户端一律收到「整份快照」，永远正确。


## 线程/任务模型（单进程，单事件循环）

- job 任务：跑 `_event_stream`，把每一帧 `publish` 给所有订阅者；
- 订阅者：`StreamingResponse` 的 body 生成器，从自己的队列取帧；
- 断开：订阅者的生成器被取消 → `detach()`，**job 不受影响**。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
from collections import deque
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# 单个订阅者的排队字节上限。超了就断开**这个订阅者**，而不是丢中间的事件 ——
# 丢掉中间一段 text_delta 会让前端显示的文字缺字，比直接断开更糟（用户会以为
# 模型写错了）。阶段 3 有了续看之后，被断开的订阅者可以重连补齐。
_SUBSCRIBER_QUEUE_LIMIT_BYTES = 4 * 1024 * 1024

# 同一个 job 同时允许的订阅者数（双标签页 + 一点余量）。
MAX_SUBSCRIBERS = 8

# 重放缓冲的上界。两条都要：条数防「一轮几万条小帧」，字节防「几百条巨型工具结果」。
# 超了丢最老的 —— 于是缓冲变成「最近 N 条」，游标太老就降级成 sync 快照。
#
# 可用环境变量压低（`HLZ_TURNJOB_REPLAY_FRAMES` / `..._BYTES`）：真机验证「溢出
# 降级 sync」这条路径时，靠正常回答挤爆 2000 条不现实（实测一轮几百帧），
# 压小上限才能确定性地触发。默认值才是生产值。
REPLAY_MAX_FRAMES = int(os.environ.get("HLZ_TURNJOB_REPLAY_FRAMES", "2000"))
REPLAY_MAX_BYTES = int(os.environ.get("HLZ_TURNJOB_REPLAY_BYTES", str(2 * 1024 * 1024)))

# 队列里的「没有更多了」标记。用独立哨兵而不是 None，避免和空帧混淆。
_STREAM_END = object()

# SSE 心跳：静默满这么久就往订阅者队列里塞一帧注释行。
#
# **静默计时而不是固定 ticker**（与 LibreChat `stream/keepalive.ts` 同思路）：
# 固定间隔在高速输出时是纯浪费 —— 我们一轮几千帧，25s 内根本不会安静，心跳一帧
# 都不需要发。只在「真的没东西可写」时才补一帧，让代理看到字节。
#
# 为什么需要它：实测直连 uvicorn 能撑过 36s 空闲，但一旦前面挂 nginx
# （`proxy_read_timeout` 默认 60s）或 Cloudflare（100s 无字节即断），一次长
# `code_run` 的空档就会把**续看连接**切掉，用户看到内容停住。Aivory 把它做成
# 默认开启就是因为不该赌部署形态（`messages_handlers.go:2009` §6.2）。
#
# 0 或负数表示关闭。用注释行（`:` 开头）而非事件：SSE 客户端按规范必须跳过，
# 不会触发任何 onEvent 分支，也不会占用 seq。
HEARTBEAT_IDLE_S = float(os.environ.get("HLZ_TURNJOB_HEARTBEAT_IDLE_S", "15"))

# 心跳注释帧。**不带 id:** —— 它不是真事件，不该推进续看游标。
_HEARTBEAT_FRAME = ": keep-alive\n\n"

# 进程内的 job 表。key = (user_id, session_id, generation_id)。
_JOBS: dict[tuple[str, str, str], "TurnJob"] = {}

# 持有 job task 的强引用：否则任务对象可能被 GC 掉，正在跑的生成会静默消失。
_JOB_TASKS: set[asyncio.Task] = set()

# 建号序（job 的 started_at）。模块级计数器，跨事件循环也不会撞号。
_START_SEQ = itertools.count(1)


def _key(user_id: str, session_id: str, generation_id: str) -> tuple[str, str, str]:
    return (str(user_id), str(session_id), str(generation_id))


class Subscriber:
    """一个 SSE 订阅者。队列保证「产出顺序 = 投递顺序」。"""

    __slots__ = ("queue", "queued_bytes", "overflow")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.queued_bytes = 0
        self.overflow = False

    def _put(self, item: Any) -> None:
        self.queue.put_nowait(item)

    def push(self, frame: str) -> None:
        """投递一帧。超过字节上限时标记溢出并唤醒消费者去收尾。"""
        if self.overflow:
            return
        self.queued_bytes += len(frame)
        if self.queued_bytes > _SUBSCRIBER_QUEUE_LIMIT_BYTES:
            # 不丢中间事件：清空待发内容并结束这条订阅，由消费者决定怎么告知用户。
            self.overflow = True
            self.queue = asyncio.Queue()
            self.queued_bytes = 0
            self.queue.put_nowait(_STREAM_END)
            logger.warning("subscriber queue overflow; closing this subscriber only")
            return
        self._put(frame)

    def end(self) -> None:
        self.queue.put_nowait(_STREAM_END)

    async def drain(self) -> AsyncIterator[str]:
        """产出帧直到 job 结束或本订阅者溢出。

        空闲超过 `HEARTBEAT_IDLE_S` 就补一帧注释行（心跳），让中间的代理看到字节。
        **只在真的没东西可写时才发** —— 有帧在流动时不发，这是「静默计时」而不是
        固定 ticker，与 LibreChat 的 `keepalive.ts` 同思路。

        心跳不经过 `publish()`，所以既**不占 seq**、也**不进重放缓冲**：它不是真事件，
        续看游标不该被它推进，客户端也不该重放它。
        """
        idle: Optional[float] = HEARTBEAT_IDLE_S if HEARTBEAT_IDLE_S > 0 else None
        while True:
            if idle is None:
                item = await self.queue.get()
            else:
                try:
                    item = await asyncio.wait_for(self.queue.get(), idle)
                except asyncio.TimeoutError:
                    # 队列等待被取消，但队列项仍留在原位（CPython 3.13 实测：不丢帧），
                    # 所以这里补一帧心跳再继续等，不会跳过任何真实事件。
                    if self.overflow:
                        return
                    yield _HEARTBEAT_FRAME
                    continue
            if item is _STREAM_END:
                return
            self.queued_bytes -= len(item)
            if self.queued_bytes < 0:  # 防御：计数不该为负
                self.queued_bytes = 0
            yield item


class TurnJob:
    """一次生成。生命周期与 HTTP 请求无关。"""

    def __init__(self, user_id: str, session_id: str, generation_id: str,
                 message_id: str = "") -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.generation_id = generation_id
        self.message_id = message_id
        self.subscribers: list[Subscriber] = []
        self.finished = False
        self.task: Optional[asyncio.Task] = None
        # 单调递增的建号序，用于「同一会话出现两个未 finish 的 job 时挑最新的」。
        # 不能用 generation_id 字符串比大小（随机 id 不可比），也不用挂钟
        # （同一毫秒内可能建两个）。见 get_live_job_for_session。
        self.started_at = next(_START_SEQ)
        # 单调递增的事件序号 = 客户端续看游标。由 job 拥有并重写每帧的 id:。
        self.seq = 0
        # 重放缓冲：[(seq, frame)]，seq 连续递增，丢最老的。
        self.replay: deque[tuple[int, str]] = deque()
        self.replay_bytes = 0
        # 读取**此刻** blocks 的回调，由 _event_stream 提供（持有就地 append 的那个
        # 列表引用）。sync 时用它序列化最新状态 —— 见模块头「为什么不存已落库的快照」。
        self.blocks_provider: Callable[[], list[dict]] = lambda: []

    # ---------------------------------------------------------------- 重放

    def _rewrite_seq(self, frame: str, seq: int) -> str:
        """把帧里的 `id: N` 换成 `id: <seq>`。

        `_event_stream` 自己的编号不连续（`healed` / 早退 error 走不带 id 的
        `sse_frame`），而续看游标必须单调无洞、且与缓冲键一致，所以由 job 统一重写。
        """
        out = []
        replaced = False
        for line in frame.split("\n"):
            if line.startswith("id:"):
                out.append(f"id: {seq}")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            # 没有 id 行的帧：补一行，位置在 data 之后（SSE 允许 id 在 data 前后）
            while out and out[-1] == "":
                out.pop()
            out.append(f"id: {seq}")
            out.append("")
        return "\n".join(out)

    def _buffer(self, seq: int, frame: str) -> None:
        self.replay.append((seq, frame))
        self.replay_bytes += len(frame)
        while (len(self.replay) > REPLAY_MAX_FRAMES
               or self.replay_bytes > REPLAY_MAX_BYTES):
            _, old = self.replay.popleft()
            self.replay_bytes -= len(old)

    def oldest_seq(self) -> int:
        """缓冲里最老的 seq。空缓冲时返回「比当前 seq 大 1」，表示没有可重放的。"""
        if not self.replay:
            return self.seq + 1
        return self.replay[0][0]

    def sync_frames(self, after: int) -> list[str]:
        """算出「客户端带 after=k 连上来」时应当先发什么。

        三种情形（对应设计文档 §4.2）：

        1. `k >= seq`：客户端已是最新（重复连接 / 刚断即连），什么都不用补；
        2. `k + 1 >= oldest_seq()`：**k 在缓冲覆盖范围内** → 纯增量重放；
        3. 否则（k 太老 / 首次连接 / 缓冲已溢出）→ 发 `sync` 快照，从此刻的状态重来。

        第 3 种情形里 `sync` 的 blocks 是**此刻**的全部状态、`id` 是**此刻**的 seq，
        两者取自同一个同步点，所以后面接实时事件必然无缝 —— 不存在「快照比缓冲旧、
        中间漏一段」的窗口（见模块头 §为什么不存已落库的快照）。
        """
        if after >= self.seq:
            return []
        if after + 1 >= self.oldest_seq():
            return [f for s, f in self.replay if s > after]
        payload = json.dumps(
            {"blocks": self.blocks_provider(), "seq": self.seq},
            ensure_ascii=False)
        return [f"event: sync\ndata: {payload}\nid: {self.seq}\n\n"]

    # ---------------------------------------------------------------- 订阅侧

    def attach(self) -> Optional[Subscriber]:
        """登记一个订阅者。返回 None 表示超过上限（调用方应回一个错误帧）。"""
        if len(self.subscribers) >= MAX_SUBSCRIBERS:
            return None
        sub = Subscriber()
        self.subscribers.append(sub)
        if self.finished:
            # job 已经结束：立刻告诉它「没有更多了」，让订阅者干净退出。
            sub.end()
        return sub

    def detach(self, sub: Subscriber) -> None:
        """订阅者离开（正常结束或客户端断开）。**不影响 job**。"""
        try:
            self.subscribers.remove(sub)
        except ValueError:
            pass

    # ---------------------------------------------------------------- 生成侧

    def publish(self, frame: str) -> None:
        """把一帧发给所有订阅者，并留进重放缓冲。没有订阅者时只进缓冲。"""
        self.seq += 1
        frame = self._rewrite_seq(frame, self.seq)
        self._buffer(self.seq, frame)
        for sub in list(self.subscribers):
            sub.push(frame)

    def set_blocks_provider(self, provider: Callable[[], list[dict]]) -> None:
        """注册「读此刻 blocks」的回调。

        **必须传一个会返回最新状态的函数，而不是一份快照**：传快照就回到
        「快照落后于缓冲」的老问题（`_persist` 250ms 节流）。
        """
        self.blocks_provider = provider

    def finish(self) -> None:
        """job 结束：通知所有订阅者之后不会再有帧，并把自己从表里摘掉。

        幂等且是**唯一收口**：正常路径由 `_wrapped` 的 finally 调，异常路径（任务
        还没开始执行就被 cancel、或 create_task 本身失败）由 `start_job` 里注册的
        done_callback 兜底。两处都调不会出问题。
        """
        if self.finished:
            return
        self.finished = True
        for sub in list(self.subscribers):
            sub.end()
        _JOBS.pop(_key(self.user_id, self.session_id, self.generation_id), None)


# -------------------------------------------------------------------- 注册表


def get_job(user_id: str, session_id: str, generation_id: str) -> Optional[TurnJob]:
    job = _JOBS.get(_key(user_id, session_id, generation_id))
    if job is not None and job.finished:
        return None
    return job


def get_live_job_for_session(user_id: str, session_id: str) -> Optional[TurnJob]:
    """该会话当前活着的 job（用于「刷新后自动接上」）。

    **同一会话可能短暂有两个未 finish 的 job**：旧 job 的 `_event_stream` 在 finally
    里先 `await release_generation()` 释放 DB 租约，然后 `_wrapped` 的 finally 才
    `job.finish()` 摘表。中间那段 await 里新请求已经能抢到租约并建新 job
    （`tools/turn_job_verify/lifecycle_probes.py` 的 P5 实测复现）。

    所以这里必须挑**代际最新**的那个，而不是「第一个扫到的」：`_JOBS` 是 dict 且
    旧键先插入，`for ... return` 会稳定返回正要收尾的旧 job，于是刷新页面接到一个
    不会再产出任何帧的流，表现为「一直在转圈、内容不动」。

    用 `started_at`（单调序）而不是字符串比 generation_id：后者是随机 id 不可比。
    """
    best: Optional[TurnJob] = None
    for (uid, sid, _gid), job in list(_JOBS.items()):
        if uid != str(user_id) or sid != str(session_id) or job.finished:
            continue
        if best is None or job.started_at > best.started_at:
            best = job
    return best


def live_job_count() -> int:
    return sum(1 for j in _JOBS.values() if not j.finished)


def start_job(job: TurnJob,
              runner: Callable[[TurnJob], Awaitable[None]]) -> TurnJob:
    """建 job 表项并启动任务。

    调用方应当**先** `attach()` 再 `start_job()`：订阅者先就位，job 的第一帧
    才不会漏（task 在下一个 await 点之前不会运行）。
    """
    _JOBS[_key(job.user_id, job.session_id, job.generation_id)] = job

    async def _wrapped() -> None:
        try:
            await runner(job)
        except asyncio.CancelledError:
            # 停止 / 关闭：不吞掉，但要把订阅者放掉，否则它们会一直等。
            raise
        except Exception:  # noqa: BLE001
            logger.warning("turn job failed", exc_info=True)
        finally:
            job.finish()

    task = asyncio.create_task(_wrapped(), name=f"turnjob:{job.generation_id}")
    job.task = task
    _JOB_TASKS.add(task)
    task.add_done_callback(_JOB_TASKS.discard)
    # 兜底收口：`_wrapped` 的 finally 只在「协程真的开始执行过」时才跑。
    # 实测（`tools/turn_job_verify/lifecycle_probes.py` 的 P1）任务建好后、第一个 await 点之前
    # 就被 cancel 的话，协程体一行都不执行，finally 也就不会跑 —— job 会永远留在
    # `_JOBS` 里，`live_job_count()` 不降，续看端拿到一个永远不会产出帧的 job 并挂死
    # （同脚本 P3 复现）。
    #
    # done_callback 在这种情形下**仍然触发**（同脚本 P2 验证），
    # 所以把它作为与执行路径无关的唯一兜底。`finish()` 幂等，两条路径都调无害。
    def _reap(_t: asyncio.Task, _job: "TurnJob" = job) -> None:
        _job.finish()
        _job.task = None   # 断开 job <-> task 的引用环，不然要靠 gc 回收

    task.add_done_callback(_reap)
    return job


async def shutdown_all(reason: str = "server shutdown") -> int:
    """进程退出时收掉所有 job（测试与 lifespan 用）。返回取消数量。"""
    n = 0
    for job in list(_JOBS.values()):
        task = job.task
        if task is not None and not task.done():
            task.cancel()
            n += 1
        job.finish()
    if n:
        logger.info("cancelled %d live turn job(s): %s", n, reason)
    _JOBS.clear()
    return n
