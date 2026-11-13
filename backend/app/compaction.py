"""Context compaction: soft-line summary (manual/auto) + hard-line trim fallback + visible record.

Operates on the dict history held by HistoryStore (no langchain message types).
Design aligns with GenericAgent core/context.py:
- soft line: summarize older messages into one, keep the recent tail;
- hard line: drop oldest messages without calling the LLM (final safety net);
- visible: write a role=system summary message to the messages table for the UI.
"""
from __future__ import annotations

import asyncio
import uuid

from . import db
from .runtime import get_hub
from .tokens import SUMMARY_SOURCE_VAL, estimate_tokens, message_text, message_token_counter

SUMMARY_PROMPT = (
    "你是对话压缩助手。请把下面这段较早的对话压缩成简洁的中文摘要，"
    "保留：用户目标、关键事实/数字/结论、已做出的决定、未完成的待办。"
    "不要臆造，不要加评论，只输出摘要正文。"
    + chr(10) + chr(10)
    + "{extra}对话：" + chr(10) + "{messages}"
)

HARD_TRIM_RECOVER_RATIO = 0.6
SOFT_LINE_RECOVER_RATIO = 0.5
AUTO_MIN_KEEP_MESSAGES = 2

# 压缩互斥锁：按 (user_id, session_id) 一把。
#
# 为什么需要：`enforce_soft_limit` / `enforce_hard_limit` 都是「读 → 算 → 写」，
# 而 `HistoryStore` 的锁只包住**单次 set()**，`get()` 完全不加锁。两个压缩
# 并发时会出现经典丢失更新：
#
#     软线: get(A) ──[调 LLM 3-5s]── set(基于 A 的摘要)
#     硬线:      get(A) ── set(基于 A 的裁剪)          ↑ 覆盖掉硬线的裁剪
#
# 后果很实际：硬线刚裁下去的消息又回来了，下一次请求仍然超窗。
# 现在压缩是顺序调用的，靠「调用顺序」隐式保证；一旦有人并行化（P3 之后
# 首帧提前会让这类并行变得自然），就会踩到这个坑。
_compaction_locks: dict[tuple[str, str], asyncio.Lock] = {}


def compaction_lock(user_id: str, session_id: str) -> asyncio.Lock:
    """取（并按需创建）该会话的压缩互斥锁。

    注意：锁只保证**进程内**互斥。多进程部署时各进程各有各的锁，
    真正的并发安全仍依赖 SQLite 的事务与 busy_timeout。
    """
    key = (str(user_id), str(session_id))
    lock = _compaction_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _compaction_locks[key] = lock
    return lock


def _dialog_text(messages, budget=24000):
    lines = []
    for m in messages:
        lines.append(str(m.get("role", "?")) + ": " + message_text(m))
    return chr(10).join(lines)[:budget]


def _summary_message(summary, trigger):
    return {
        "role": "user",
        "content": "[上下文摘要（较早对话已压缩，trigger=" + trigger + "）]" + chr(10) + summary,
        "_source": SUMMARY_SOURCE_VAL,
        "id": "summary_" + uuid.uuid4().hex[:12],
    }


def _trim_note_message(dropped):
    return {
        "role": "user",
        "content": "[已自动裁剪 " + str(dropped) + " 条较早消息以控制上下文长度（硬线兜底）]",
        "_source": "hard_trim",
        "id": "trim_" + uuid.uuid4().hex[:12],
    }


async def _summarize(ur, prompt: str, *, max_tokens: int | None = None) -> str:
    """Call the user's model to produce a summary; return '' on any failure."""
    try:
        res = await ur.model.client.run(
            [{"role": "user", "content": prompt}], max_tokens=max_tokens
        )
        return (res.content or "").strip()
    except TypeError:
        # Older/fake clients without the per-call cap.
        try:
            res = await ur.model.client.run([{"role": "user", "content": prompt}])
            return (res.content or "").strip()
        except Exception:
            return ""
    except Exception:
        return ""


async def _record_visible_summary(session_id, user_id, summary, trigger, summary_tokens, kept, dropped):
    try:
        await db.add_message(
            session_id, "system", content="",
            blocks=[{
                "type": "summary", "text": summary, "trigger": trigger,
                "summary_tokens": summary_tokens, "kept_messages": kept, "dropped_messages": dropped,
            }],
            status="complete",
        )
    except Exception:
        pass
    try:
        await db.update_session(
            session_id, user_id,
            compaction={"compacted": True, "summary_tokens": summary_tokens,
                        "kept_messages": kept, "trigger": trigger},
        )
    except Exception:
        pass


def _safe_keep_from(messages, keep_from):
    """Don't start the preserved tail on a tool message (its assistant tool_call
    would be summarized away, leaving an orphan tool result)."""
    n = len(messages)
    if keep_from < 0:
        keep_from = 0
    while 0 < keep_from < n and messages[keep_from].get("role") == "tool":
        keep_from -= 1
    return max(0, keep_from)


def _tail_keep_from(messages, keep_from, budget):
    """Advance ``keep_from`` until the preserved suffix fits ``budget`` tokens.

    Used only as a last resort when the summary alone keeps the history above
    the soft line.  Returns ``None`` when no safe boundary fits (e.g. a single
    huge message), so the caller can keep the summary + full tail instead.
    """
    n = len(messages)
    k = _safe_keep_from(messages, keep_from)
    for _ in range(n):
        if message_token_counter(messages[k:]) <= budget:
            return k
        if k >= n - 1:
            return None
        nxt = _safe_keep_from(messages, k + 1)
        if nxt <= k:
            return None
        k = nxt
    return None


async def manual_compact(user_id, session_id, keep=6, instructions=""):
    """Manual compaction (/compact + toolbar button).

    与自动压缩共用会话级锁：用户点「压缩」时若那一轮正好在自动压缩，
    两边并发写回会互相覆盖。
    """
    async with compaction_lock(user_id, session_id):
        return await _manual_compact_locked(user_id, session_id, keep, instructions)


async def _manual_compact_locked(user_id, session_id, keep=6, instructions=""):
    hub = get_hub()
    if hub is None:
        return {"compacted": False, "trigger": "manual", "message": "runtime 未就绪"}
    sess = await db.get_session(session_id, user_id)
    model_id = (sess or {}).get("model_id", "") or ""
    ur = await hub.get_user_runtime(user_id, model_id)
    history = await ur.store.get(user_id, session_id)
    total = len(history)
    if total <= keep:
        return {"compacted": False, "trigger": "manual", "dropped_messages": 0,
                "kept_messages": total, "summary_tokens": 0, "before_messages": total,
                "after_messages": total,
                "message": "消息太少（" + str(total) + " 条，≤ 保留阈值 " + str(keep) + "），无需压缩"}

    keep_from = _safe_keep_from(history, max(0, total - keep))
    old = history[:keep_from]
    preserved = history[keep_from:]
    if not old:
        return {"compacted": False, "trigger": "manual", "dropped_messages": 0,
                "kept_messages": total, "summary_tokens": 0, "before_messages": total,
                "after_messages": total,
                "message": "消息太少（" + str(total) + " 条），无需压缩"}
    extra = ("补充要求：" + instructions.strip() + chr(10)) if (instructions and instructions.strip()) else ""
    dialog = _dialog_text(old)
    summary = await _summarize(ur, SUMMARY_PROMPT.format(extra=extra, messages=dialog))
    if not summary:
        return {"compacted": False, "trigger": "manual", "dropped_messages": 0,
                "kept_messages": total, "summary_tokens": 0, "before_messages": total,
                "after_messages": total, "message": "摘要失败或为空，未压缩"}

    sm = _summary_message(summary, "manual")
    new_history = [sm, *preserved]
    await ur.store.set(user_id, session_id, new_history)

    dropped = len(old)
    kept = len(preserved) + 1
    summary_tokens = estimate_tokens(summary)
    await _record_visible_summary(session_id, user_id, summary, "manual", summary_tokens, kept, dropped)
    return {"compacted": True, "trigger": "manual", "dropped_messages": dropped,
            "kept_messages": kept, "summary_tokens": summary_tokens,
            "before_messages": total, "after_messages": kept,
            "message": "已压缩 " + str(dropped) + " 条较早消息 → 摘要约 " + str(summary_tokens)
                       + " tokens；保留 " + str(kept) + " 条（最近 "
                       + str(len(preserved)) + " 条 + 1 条摘要）"}


async def enforce_soft_limit(user_id, session_id, pending_text=""):
    """Soft-line auto compaction before a turn. Returns stats dict or None.

    外层持**会话级压缩锁**：整个「读 → 摘要 → 写」序列必须互斥，
    否则并发的硬线裁剪会被这里的写回覆盖（见 `compaction_lock` 的说明）。
    锁**故意包住 LLM 摘要调用**——摘要基于的输入必须与最终写回一致，
    中途放锁等于允许别人在中间改历史。
    """
    async with compaction_lock(user_id, session_id):
        return await _enforce_soft_limit_locked(user_id, session_id, pending_text)


async def _enforce_soft_limit_locked(user_id, session_id, pending_text=""):
    hub = get_hub()
    if hub is None:
        return None
    sess = await db.get_session(session_id, user_id)
    model_id = (sess or {}).get("model_id", "") or ""
    ur = await hub.get_user_runtime(user_id, model_id)
    soft = int(getattr(ur, "soft_limit", 0) or 0)
    if soft <= 0:
        return None
    messages = await ur.store.get(user_id, session_id)
    total = message_token_counter(messages) + estimate_tokens(pending_text or "")
    if total <= soft:
        return None

    target = max(1, int(soft * SOFT_LINE_RECOVER_RATIO))
    keep_from = len(messages)
    acc = 0
    for i in range(len(messages) - 1, -1, -1):
        acc += message_token_counter([messages[i]])
        if acc > target:
            keep_from = i + 1
            break
        keep_from = i
    keep_from = min(keep_from, max(0, len(messages) - AUTO_MIN_KEEP_MESSAGES))
    keep_from = _safe_keep_from(messages, keep_from)

    old = messages[:keep_from]
    preserved = messages[keep_from:]
    if not old:
        return None

    # The summary must bring the history back under the soft line, otherwise
    # the very next turn compacts again (BUG-06: "after_tokens > soft").  Cap
    # the summary call and retry with a smaller tail when needed.
    attempts = 0
    sm = None
    st = 0
    after = 0
    while True:
        tail_tokens = message_token_counter(preserved)
        # Cap the summary inside the recovery budget, not just the soft line:
        # keeping ~25% headroom is what stops every following turn from
        # compacting again (BUG-06).
        cap = max(128, min(target, soft - 32) - tail_tokens - 32)
        summary = await _summarize(
            ur, SUMMARY_PROMPT.format(extra="", messages=_dialog_text(old)),
            max_tokens=min(4096, cap),
        )
        if not summary:
            return None
        sm = _summary_message(summary, "auto")
        st = estimate_tokens(summary)
        after = message_token_counter(preserved) + st
        if after <= soft or attempts >= 2 or not preserved:
            break
        # Fold the oldest half of the preserved tail into the next summary
        # pass; never start the tail on an orphan tool message.
        move_to = _safe_keep_from(messages, keep_from + max(1, len(preserved) // 2))
        if move_to <= keep_from:
            break
        keep_from = move_to
        old = messages[:keep_from]
        preserved = messages[keep_from:]
        attempts += 1

    if after > soft and preserved:
        # Last resort: drop raw tail messages (without summarising) so the
        # next turn cannot compact on the same history again.
        limit = _tail_keep_from(messages, keep_from, max(0, soft - st))
        if limit is not None and limit > keep_from:
            keep_from = limit
            preserved = messages[keep_from:]
            after = message_token_counter(preserved) + st

    await ur.store.set(user_id, session_id, [sm, *preserved])
    dropped = len(old)
    kept = len(preserved) + 1
    await _record_visible_summary(session_id, user_id, summary, "auto", st, kept, dropped)
    return {"trigger": "auto", "dropped_messages": dropped, "kept_messages": kept,
            "summary_tokens": st, "before_tokens": total, "after_tokens": after, "soft_limit": soft}


async def enforce_hard_limit(user_id, session_id):
    """Hard-line: drop oldest messages (no LLM). Returns stats or None.

    与软线共用同一把会话级压缩锁：两者都是「读 → 改 → 写」，
    并发会丢失更新（硬线刚裁的消息被软线的写回覆盖 -> 下次仍超窗）。
    """
    async with compaction_lock(user_id, session_id):
        return await _enforce_hard_limit_locked(user_id, session_id)


async def _enforce_hard_limit_locked(user_id, session_id):
    hub = get_hub()
    if hub is None:
        return None
    sess = await db.get_session(session_id, user_id)
    model_id = (sess or {}).get("model_id", "") or ""
    ur = await hub.get_user_runtime(user_id, model_id)
    hard = int(getattr(ur, "hard_limit", 0) or 0)
    if hard <= 0:
        return None
    messages = await ur.store.get(user_id, session_id)
    if not messages:
        return None
    total_tokens = message_token_counter(messages)
    if total_tokens <= hard:
        return None

    target = int(hard * HARD_TRIM_RECOVER_RATIO)
    keep_from = len(messages)
    acc = 0
    for i in range(len(messages) - 1, -1, -1):
        acc += message_token_counter([messages[i]])
        if acc > target:
            keep_from = i + 1
            break
        keep_from = i
    if keep_from <= 0:
        keep_from = max(1, len(messages) - 2)
    keep_from = _safe_keep_from(messages, keep_from)
    dropped = keep_from
    preserved = messages[keep_from:]
    if not preserved:
        preserved = messages[-1:]
        dropped = len(messages) - 1

    note = _trim_note_message(dropped)
    await ur.store.set(user_id, session_id, [note, *preserved])
    after = message_token_counter(preserved) + estimate_tokens(message_text(note))
    await _record_visible_summary(session_id, user_id, message_text(note), "hard_trim",
                                  estimate_tokens(message_text(note)), len(preserved), dropped)
    return {"trigger": "hard_trim", "dropped_messages": dropped, "kept_messages": len(preserved),
            "summary_tokens": estimate_tokens(message_text(note)),
            "before_tokens": total_tokens, "after_tokens": after, "hard_limit": hard}
