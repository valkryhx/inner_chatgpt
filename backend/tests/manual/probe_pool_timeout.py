"""诊断 PoolTimeout 的真实来源。

现象：任务跑到一半报 `network error: PoolTimeout`，4 次重试全失败。
但直连测试三个 LLM 配置并发 5 次都正常。

要区分三种可能：
  A. 上游限流（返回 429 / 空响应）
  B. 本机 httpx 连接池真的被打满（并发超过 32）
  C. 流式读取阶段卡住（连接被占着不释放，直到 pool 超时）

判据：
  - 用真实流式请求测，看首字延迟与完成情况
  - 记录同时打开的连接数
  - 观察一个流式请求最长能占多久

用法：
    python tests/manual/probe_pool_timeout.py
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))


def configs() -> list[dict]:
    import os
    db = BACKEND / "data" / "app.db"
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "select label, base_url, model, api_key, wire_api from user_llm_configs "
        "where user_id='u_c56b8337dd09'").fetchall()]
    c.close()
    return rows


async def stream_one(cfg: dict, prompt: str, timeout: float) -> dict:
    """发一个流式请求，记录首字延迟、总耗时、是否完整结束。"""
    import httpx

    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 200,
    }
    headers = {"Authorization": "Bearer " + cfg["api_key"],
               "Content-Type": "application/json"}

    t0 = time.time()
    first = None
    chunks = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0, pool=10.0),
                                     limits=httpx.Limits(max_connections=32)) as cli:
            async with cli.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    return {"ok": False, "err": f"HTTP {resp.status_code}",
                            "t": time.time() - t0}
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        raw = line[6:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            d = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if first is None and d.get("choices"):
                            first = time.time() - t0
                        chunks += 1
        return {"ok": True, "first": first, "chunks": chunks,
                "t": time.time() - t0}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "err": f"{type(exc).__name__}: {str(exc)[:80]}",
                "t": time.time() - t0, "first": first, "chunks": chunks}


async def main() -> int:
    cfgs = configs()
    print(f"配置数 {len(cfgs)}\n")

    prompt = ("请用 Python 写一个 8 皇后的完整实现，包含回溯算法、"
              "解的计数、以及打印第一个解的函数。代码要完整可运行。")

    # ---- 1. 单个流式请求 ----
    print("== 1. 单个流式请求（看首字延迟与完整性）==")
    for cfg in cfgs:
        r = await stream_one(cfg, prompt, 120)
        if r["ok"]:
            print(f"  {cfg['label']}: 首字 {r['first']:.2f}s, "
                  f"总 {r['t']:.1f}s, {r['chunks']} 块")
        else:
            print(f"  {cfg['label']}: FAIL {r['err']} ({r['t']:.1f}s)")

    # ---- 2. 并发流式（模拟多智能体）----
    print("\n== 2. 并发 6 个流式请求（模拟多智能体同时跑）==")
    for cfg in cfgs:
        t0 = time.time()
        results = await asyncio.gather(*[stream_one(cfg, prompt, 180) for _ in range(6)])
        ok = sum(1 for r in results if r["ok"])
        fails = [r.get("err", "") for r in results if not r["ok"]]
        firsts = [f"{r['first']:.1f}" for r in results if r.get("first")]
        print(f"  {cfg['label']}: {ok}/6 成功，总耗时 {time.time()-t0:.1f}s")
        if firsts:
            print(f"    首字延迟: {firsts}")
        if fails:
            print(f"    失败: {fails[:3]}")

    # ---- 3. 长输出（看会不会中途断）----
    print("\n== 3. 长输出请求（max_tokens=2000，看流是否完整）==")
    long_prompt = ("请详细解释 Python 的 GIL，包括它的历史、对多线程的影响、"
                   "在 3.13 的变化、以及实际工程中应该怎么应对。"
                   "要求：2000 字以上，分章节。")
    for cfg in cfgs:
        r = await stream_one(cfg, long_prompt, 240)
        if r["ok"]:
            print(f"  {cfg['label']}: 首字 {r['first']:.2f}s, 总 {r['t']:.1f}s, "
                  f"{r['chunks']} 块")
        else:
            print(f"  {cfg['label']}: FAIL {r['err']} ({r['t']:.1f}s)")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))