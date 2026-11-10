"""只读工具并发、写工具串行（TDD 红先）。"""
from __future__ import annotations

import asyncio
import time

PARALLEL = frozenset({"file_read", "file_glob", "file_grep", "list_files"})


def _calls(*names):
    return [{"id": f"c{i}", "name": n, "args": {}} for i, n in enumerate(names)]


def test_plan_batches_groups_adjacent_reads():
    from app.tool_batching import plan_batches

    plan = plan_batches(_calls("file_read", "file_grep", "file_write", "file_read"), PARALLEL)
    assert [len(b) for b in plan] == [2, 1, 1]


def test_parallel_reads_are_faster_than_serial():
    from app.tool_batching import run_batched

    async def dispatch(name, args):
        await asyncio.sleep(0.06)
        return name

    calls = _calls("file_read", "file_grep", "list_files")
    t0 = time.perf_counter()
    out = asyncio.run(run_batched(calls, dispatch, PARALLEL))
    elapsed = time.perf_counter() - t0
    assert out == ["file_read", "file_grep", "list_files"]
    assert elapsed < 0.15


def test_write_tools_stay_serial():
    from app.tool_batching import run_batched

    async def dispatch(name, args):
        await asyncio.sleep(0.05)
        return name

    calls = _calls("file_write", "file_patch", "file_write")
    t0 = time.perf_counter()
    asyncio.run(run_batched(calls, dispatch, PARALLEL))
    assert time.perf_counter() - t0 >= 0.14


def test_concurrency_is_capped():
    from app.tool_batching import run_batched

    active = 0
    peak = 0

    async def dispatch(name, args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.03)
        active -= 1
        return name

    calls = _calls(*(["file_read"] * 10))
    asyncio.run(run_batched(calls, dispatch, PARALLEL, max_parallel=4))
    assert peak <= 4


def test_mixed_sequence_keeps_original_order():
    from app.tool_batching import run_batched

    async def dispatch(name, args):
        await asyncio.sleep(0.01 if name in PARALLEL else 0.02)
        return name.upper()

    calls = _calls("file_read", "file_write", "file_grep")
    out = asyncio.run(run_batched(calls, dispatch, PARALLEL))
    assert out == ["FILE_READ", "FILE_WRITE", "FILE_GREP"]


def test_code_run_is_never_parallel():
    from app.tool_batching import plan_batches

    plan = plan_batches(_calls("file_read", "code_run", "file_read"), PARALLEL)
    assert [len(b) for b in plan] == [1, 1, 1]
