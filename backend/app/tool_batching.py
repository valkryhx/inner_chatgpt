"""工具批处理：相邻的只读工具并发，写类与 code_run 严格串行。

为什么不全部并发：file_write / file_patch / code_run 之间有隐式顺序依赖
（先写后跑测试），并发会破坏语义；MCP 工具的行为未知，也不放进来。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Sequence

PARALLEL_SAFE_TOOLS = frozenset({"file_read", "file_glob", "file_grep", "list_files"})
DEFAULT_MAX_PARALLEL = 4


def plan_batches(calls: Sequence[dict], parallel_safe=frozenset(PARALLEL_SAFE_TOOLS)) -> list[list[int]]:
    """把调用序列切成批次；相邻的只读调用合成一批，其余各自一批。"""
    batches: list[list[int]] = []
    current: list[int] = []
    for i, call in enumerate(calls):
        if call.get("name") in parallel_safe:
            current.append(i)
            continue
        if current:
            batches.append(current)
            current = []
        batches.append([i])
    if current:
        batches.append(current)
    return batches


async def run_batched(
    calls: Sequence[dict],
    dispatch: Callable[[str, dict], Awaitable[str]],
    parallel_safe=frozenset(PARALLEL_SAFE_TOOLS),
    max_parallel: int = DEFAULT_MAX_PARALLEL,
) -> list[str]:
    """按批次执行，返回与 calls 同序的结果。"""
    results: list[Any] = [None] * len(calls)
    for batch in plan_batches(calls, parallel_safe):
        if len(batch) == 1:
            i = batch[0]
            results[i] = await dispatch(calls[i]["name"], calls[i].get("args") or {})
            continue
        sem = asyncio.Semaphore(max(1, int(max_parallel)))

        async def _one(idx: int) -> tuple[int, str]:
            async with sem:
                value = await dispatch(calls[idx]["name"], calls[idx].get("args") or {})
                return idx, value

        for idx, value in await asyncio.gather(*(_one(i) for i in batch)):
            results[idx] = value
    return results
