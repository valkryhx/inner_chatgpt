"""S-M6/S-M7 真实链路 e2e：走主对话 SSE，验证选择器在路上能注入。

与 selector_bench.py 的分工：
- bench 直接调 `select_slugs`（被测单元），量命中率；
- 这里走完整 SSE（创建会话 -> 发消息 -> 读事件），验证**接线**：
  选择器在 AgentLoop 的两个挂点上真的跑起来了，且注入事件真的发出去了。
"""
import asyncio, json, sys, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")
import httpx
from tests.e2e.harness import BASE, ensure_user, new_session, send_message, summarize

STATE = "SEL_SENTINEL_7731"

async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=300) as c:
        tok = await ensure_user(c, f"sm6_{int(time.time())}")
        h = {"Authorization": f"Bearer {tok}"}
        # 一条与查询**无字面重合**的记忆：查询问「同时保存报错」，记忆讲「并发写入 database is locked」
        await c.post("/api/me/memories", json={
            "slug": "db-lock-e2e",
            "title": "SQLite 并发处置",
            "description": f"SQLite 并发写入遇到 database is locked 的处置方式（标记 {STATE}）",
            "content": f"# 处置\n\n并发写入报 database is locked 时先重试。标记 {STATE}\n",
            "kind": "project"}, headers=h)

        sid = await new_session(c, tok, "S-M6")
        t0 = time.monotonic()
        ev = await send_message(c, tok, sid,
            "两个人同时保存的时候报的那个错的原文是什么来着？", 240)
        total_ms = (time.monotonic() - t0) * 1000
        s = summarize(ev)
        injected = [e for e in ev if e.get("type") == "message_injected"]
        mem_blocks = [e for e in injected
                      if "记忆" in str(e.get("text") or "")]

        print(json.dumps({
            "total_ms": round(total_ms),
            "tool_steps": s["tools"],
            "injected_count": len(injected),
            "memory_blocks": len(mem_blocks),
            "memory_text": [str(b.get("text",""))[:160] for b in mem_blocks],
            "answer": s["text"][:300],
        }, ensure_ascii=False, indent=2))
        return 0 if mem_blocks else 1

sys.exit(asyncio.run(main()))
