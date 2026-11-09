"""In-process task event log + subscriber queues for A2A streaming.

A task is owned by its background asyncio task.  An SSE request is only a
subscriber: closing it must not cancel the task.  The bounded log also lets a
late SendStreamingMessage/SubscribeToTask replay what it missed.
"""
from __future__ import annotations

import asyncio
from typing import Any

MAX_EVENTS = 4000
_DROPPED = 2000
MAX_FINISHED_TASKS = 200

_LOGS: dict[str, list[dict[str, Any]]] = {}
_SEQ: dict[str, int] = {}
_SUBS: dict[str, set[asyncio.Queue]] = {}
_DONE: set[str] = set()


def publish(task_id: str, event: dict[str, Any]) -> None:
    seq = _SEQ.get(task_id, 0) + 1
    _SEQ[task_id] = seq
    item = {"seq": seq, "event": event}
    log = _LOGS.setdefault(task_id, [])
    log.append(item)
    if len(log) > MAX_EVENTS:
        del log[:_DROPPED]
    for q in list(_SUBS.get(task_id, ())):
        q.put_nowait(item)
    if event.get("type") == "terminal":
        _DONE.add(task_id)
        _trim_finished()


def _trim_finished() -> None:
    finished = [tid for tid in _LOGS if tid in _DONE and not _SUBS.get(tid)]
    while len(_LOGS) > MAX_FINISHED_TASKS and finished:
        tid = finished.pop(0)
        _LOGS.pop(tid, None)
        _SEQ.pop(tid, None)
        _DONE.discard(tid)


def subscribe(task_id: str) -> tuple[asyncio.Queue, list[dict[str, Any]], bool]:
    """Subscribe first, then snapshot the log.  No await => no interleaving."""
    q: asyncio.Queue = asyncio.Queue()
    _SUBS.setdefault(task_id, set()).add(q)
    log = list(_LOGS.get(task_id, []))
    return q, log, task_id in _DONE


def unsubscribe(task_id: str, q: asyncio.Queue) -> None:
    subs = _SUBS.get(task_id)
    if not subs:
        return
    subs.discard(q)
    if not subs:
        _SUBS.pop(task_id, None)


def is_done(task_id: str) -> bool:
    return task_id in _DONE
