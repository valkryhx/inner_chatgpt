"""真实重试行为的端到端验证（不是 mock，走真 HTTP）。

单测用 `_no_sleep` mock 掉了 asyncio.sleep，所以只能证明「逻辑分支对」，
不能证明「真的等过、真的重发过」。这里用**真实 sleep**（缩到很小）
打一个本地假上游，观察：
- 请求次数（是否真的重发）
- 实际等待（退避是否生效）
- Retry-After 是否被遵守

用法：
    python tests/manual/probe_llm_retry_real.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


SSE_OK = (
    'data: {"choices":[{"delta":{"content":"好了"},"index":0}]}\n\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n'
    "data: [DONE]\n\n"
)


async def serve(handler_fn, port: int):
    """起一个极简 HTTP 服务，用 handler_fn 决定每次响应。"""
    from aiohttp import web

    async def dispatch(request):
        status, headers, body = handler_fn()
        return web.Response(status=status, headers=headers, text=body,
                            content_type="text/event-stream")

    app = web.Application()
    app.router.add_post("/v1/chat/completions", dispatch)
    app.router.add_post("/v1/responses", dispatch)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner


def build_client(base_url: str, max_retries: int = 3):
    from app.llm_client import LLMClient, LLMSettings
    s = LLMSettings(
        base_url=base_url, api_key="k", model="m",
        wire_api="openai_chat", timeout=10, max_retries=max_retries,
    )
    return LLMClient(s)


async def main() -> int:
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        print("需要 aiohttp（后端已装，检查环境）")
        return 2

    port = 18765
    base = f"http://127.0.0.1:{port}"

    # ---------- 1. 连续 503，第 3 次成功 ----------
    print("\n== 1. 503 -> 503 -> 200（验证真的重发）==")
    state = {"n": 0}

    def h1():
        state["n"] += 1
        if state["n"] <= 2:
            return 503, {}, "overloaded"
        return 200, {}, SSE_OK

    runner = await serve(h1, port)
    try:
        c = build_client(base)
        t0 = time.perf_counter()
        try:
            res = await c.run([{"role": "user", "content": "hi"}])
            elapsed = time.perf_counter() - t0
            record("503 两次后成功", state["n"] == 3 and res.content == "好了",
                   f"请求 {state['n']} 次，耗时 {elapsed:.2f}s，内容={res.content!r}")
            record("退避真的等了（不是 0 秒）", elapsed > 0.5,
                   f"{elapsed:.2f}s")
        except Exception as exc:  # noqa: BLE001
            record("503 两次后成功", False, f"{type(exc).__name__}: {exc}")
    finally:
        await runner.cleanup()

    # ---------- 2. Retry-After 被遵守 ----------
    print("\n== 2. 429 + Retry-After: 2（验证听上游的）==")
    state2 = {"n": 0, "times": []}

    def h2():
        state2["n"] += 1
        state2["times"].append(time.perf_counter())
        if state2["n"] == 1:
            return 429, {"retry-after": "2"}, "slow down"
        return 200, {}, SSE_OK

    runner = await serve(h2, port + 1)
    try:
        c = build_client(f"http://127.0.0.1:{port+1}")
        res = await c.run([{"role": "user", "content": "hi"}])
        if len(state2["times"]) >= 2:
            gap = state2["times"][1] - state2["times"][0]
            record("429 后按 Retry-After 等待", gap >= 1.8,
                   f"实际间隔 {gap:.2f}s（要求 >=2s，允许 0.2s 误差）")
        else:
            record("429 后按 Retry-After 等待", False, "没有重发")
        record("429 重试后成功", res.content == "好了", res.content)
    finally:
        await runner.cleanup()

    # ---------- 3. 400 绝不重试 ----------
    print("\n== 3. 400（验证不该重试的不重试）==")
    state3 = {"n": 0}

    def h3():
        state3["n"] += 1
        return 400, {}, "bad request"

    runner = await serve(h3, port + 2)
    try:
        c = build_client(f"http://127.0.0.1:{port+2}")
        try:
            await c.run([{"role": "user", "content": "hi"}])
            record("400 直接失败", False, "居然成功了")
        except Exception:  # noqa: BLE001
            record("400 不重试（只发 1 次）", state3["n"] == 1,
                   f"请求 {state3['n']} 次")
    finally:
        await runner.cleanup()

    # ---------- 4. 429 一直失败，验证放弃是有界的 ----------
    print("\n== 4. 429 一直失败（验证放弃有界，不无限重试）==")
    state4 = {"n": 0}

    def h4():
        state4["n"] += 1
        return 429, {"retry-after": "0.3"}, "always limited"

    runner = await serve(h4, port + 3)
    try:
        c = build_client(f"http://127.0.0.1:{port+3}", max_retries=2)
        t0 = time.perf_counter()
        try:
            await c.run([{"role": "user", "content": "hi"}])
            record("一直 429 最终放弃", False, "居然成功了")
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - t0
            record("一直 429 最终放弃（有界）", state4["n"] <= 3,
                   f"请求 {state4['n']} 次后放弃，耗时 {elapsed:.2f}s")
    finally:
        await runner.cleanup()

    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n===== {len(RESULTS)-len(bad)}/{len(RESULTS)} 通过 =====")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))