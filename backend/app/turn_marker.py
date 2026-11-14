"""崩溃标记：区分「用户主动停止」与「进程崩溃中断」两种悬空 tool_call。

## 为什么需要这个标记

`agent_loop.sanitize()` 修悬空 tool_call 时，如果缺结果的是**尾部**且工具名
还能解析，会**真的重新 dispatch 执行一次**（`agent_loop.py:270-281`）。

这个行为在「用户点停止」时是合理的：那一轮是用户主动打断的，工具几乎
肯定还没跑完，重放相当于替用户把这一步补上。

但**进程崩溃**时它是有害的：那个 `code_run` 可能已经执行了 —— 写了一半文件、
发了一条消息、调了一次外部接口。重放 = 重复副作用，而且没有幂等保证。

崩溃后我们**原理上无法确定**它执行到哪一步（这正是为什么不能靠猜）。
所以唯一诚实的做法是：

1. 记下「这一轮是被崩溃打断的」；
2. 修复历史时补一条**明确的「执行状态未知」**结果，而不是静默重放；
3. 让模型据此决定要不要重试、要不要先核对。

## 为什么落库而不是放内存

标记的存在意义就是「跨进程存活」。放内存的话，进程一死标记就没了，
等于没做。所以它必须在 SQLite 里。

## 谁写、谁清

- **写**：进程启动时，把上一进程遗留的未完成轮次标出来（与
  `mark_stale_streaming_stopped` / `mark_interrupted_tasks` 同一时机）；
- **清**：一轮**正常结束**后清掉。注意「正常结束」包括被用户停止 ——
  用户停止是显式动作，下一轮重放是安全的。

⚠️ 因此标记的语义必须精确：**它只代表「上一进程没有走完这一轮就死了」**，
不代表「上一轮失败了」。失败是任务自己的状态，不需要特殊处理。
"""
from __future__ import annotations

import time
from typing import Any, Optional

import aiosqlite

from .config import DB_PATH

_CREATE = """
CREATE TABLE IF NOT EXISTS turn_crash_markers (
    session_id  TEXT PRIMARY KEY,
    last_tool   TEXT NOT NULL DEFAULT '',
    steps       INTEGER NOT NULL DEFAULT 0,
    marked_at   INTEGER NOT NULL
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


async def init_tables() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.executescript(_CREATE)
        await conn.commit()


async def mark_crashed(session_id: str, *, last_tool: str = "",
                       steps: int = 0) -> None:
    """记下「这个会话的上一轮被进程崩溃打断」。"""
    if not session_id:
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute(
            "INSERT INTO turn_crash_markers(session_id,last_tool,steps,marked_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "last_tool=excluded.last_tool, steps=excluded.steps, "
            "marked_at=excluded.marked_at",
            (session_id, str(last_tool or ""), int(steps), now_ms()),
        )
        await conn.commit()


async def get_crash_marker(session_id: str) -> Optional[dict[str, Any]]:
    """读标记；没有则返回 None（这是「可以安全重放」的判据）。"""
    if not session_id:
        return None
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM turn_crash_markers WHERE session_id=?", (session_id,))
        row = await cur.fetchone()
    return dict(row) if row else None


async def clear_crash_marker(session_id: str) -> None:
    """一轮正常结束后清掉。幂等。"""
    if not session_id:
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "DELETE FROM turn_crash_markers WHERE session_id=?", (session_id,))
        await conn.commit()


async def mark_all_unfinished_crashed(reason: str = "server restart") -> int:
    """启动时：把上一进程遗留的未完成轮次全部标记为「崩溃中断」。

    判据是 `messages` 表里还有 `status='streaming'` 的 assistant 消息 ——
    那正是「这一轮没走完」的证据。这个判断要在把它们收敛成 `stopped`
    **之前**做，否则就分不出「用户停止」和「崩溃」了。

    返回标记数量（启动日志用）。
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000;")
        cur = await conn.execute(
            "SELECT DISTINCT session_id FROM messages "
            "WHERE role='assistant' AND status='streaming'"
        )
        sessions = [r["session_id"] for r in await cur.fetchall()]
    for sid in sessions:
        await mark_crashed(sid, last_tool="", steps=0)
    return len(sessions)


def describe_unknown_result(tool_name: str) -> str:
    """给模型看的「执行状态未知」说明。

    三条必须说清（顺序也是有意的）：

    1. **状态未知** —— 不是成功也不是普通失败，是「不知道」；
    2. **可能已执行** —— 这是重试前必须先核对的理由；
    3. **怎么核对** —— 给出可执行的下一步，而不是空警告。

    刻意不写成错误（`is_error` 语义）：它不是工具的失败，是观测的缺失。
    模型看到「错误」会倾向于重试，看到「未知」才会先去看一眼。
    """
    name = tool_name or "该工具"
    return (
        f"[执行状态未知] {name} 在上一轮被进程中断，无法确定是否已执行完成。"
        f"它**可能已经产生了实际影响**（文件写入、命令执行、外部调用）。"
        f"重试前请先核对结果（例如读取目标文件、查看命令产物）；"
        f"若已生效，不要再执行一次。"
    )
