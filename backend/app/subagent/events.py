"""事件缓冲与停止信号。

设计要点（设计文档 §5.3 / §9.1）：
- 主代理事件与子代理事件**共用一个队列**，由 SSE 层单点消费 → 顺序天然正确。
- StopSignal 抽出来，将来换 Redis 实现时调用方无需改动。
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator


class StopSignal:
    """可替换的停止信号（v1 = asyncio.Event）。"""

    __slots__ = ("_ev",)

    def __init__(self) -> None:
        self._ev = asyncio.Event()

    def set(self) -> None:
        self._ev.set()

    def is_set(self) -> bool:
        return self._ev.is_set()

    @property
    def event(self) -> asyncio.Event:
        return self._ev

    async def wait(self) -> None:
        await self._ev.wait()


_END = object()


class EventBuffer:
    """一个会话一次生成的统一事件队列（主 + 子）。"""

    __slots__ = ("_q",)

    def __init__(self) -> None:
        self._q: asyncio.Queue = asyncio.Queue()

    def put(self, ev: dict[str, Any]) -> None:
        self._q.put_nowait(ev)

    def close(self) -> None:
        self._q.put_nowait(_END)

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._q.get()
            if item is _END:
                return
            yield item
