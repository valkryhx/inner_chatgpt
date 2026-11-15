"""核实：客户端断开 SSE 后，生成还在不在跑？（只读产品代码）

这个问题决定「先做 SSE 续传 还是 先做自动续跑」。如果刷新页面会把生成
当场掐死，那两者都不是第一优先级 —— 先得让它别死。

判据：断开后隔 25 秒再查 DB 里 assistant 消息的 **content 长度**。
  - 长度还在涨 → 生成继续，只是看不到（那续传就是纯 UI 问题）
  - 长度不变 + status=stopped → 生成被掐死（那是数据/体验的实质损失）

顺便验第二件事：刷新后重新发消息会不会被 generation 租约拦住。

跑法：cd backend && python ../tools/persistence_verify/probe_disconnect.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000"
RID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"
DB = ROOT / "backend" / "data" / "app.db"


def snapshot(sid: str) -> list[tuple]:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT role, status, length(content) n FROM messages "
        "WHERE session_id=? ORDER BY rowid", (sid,)).fetchall()
    con.close()
    return [(r["role"], r["status"], r["n"]) for r in rows]


async def main() -> int:
    from tests.e2e.harness import ensure_user, ensure_zhanlu

    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        tok = await ensure_user(c, f"dc_{RID}".replace("-", "_"))
        await ensure_zhanlu(c, tok)
        h = {"Authorization": f"Bearer {tok}"}
        sid = (await c.post("/api/sessions", json={"title": f"DC-{RID}"},
                            headers=h)).json()["id"]
        print(f"session = {sid}")

        prompt = ("用 code_run 执行：分 6 次 sleep，每次 6 秒，每次打印一行进度 "
                  "(STEP1..STEP6)，最后总结。必须真的分 6 次，不要一次睡完。")

        # ---- 1) 发消息，只读几行就断开 ----
        print("发出消息，读 3 行后主动断开……")
        got_lines = 0
        try:
            async with c.stream("POST", f"/api/sessions/{sid}/messages",
                                json={"text": prompt},
                                headers={**h, "Accept": "text/event-stream"},
                                timeout=200) as r:
                async for line in r.aiter_lines():
                    if line.strip():
                        got_lines += 1
                        if got_lines >= 3:
                            break
        except Exception as exc:  # noqa: BLE001
            print("  (断开时异常，正常)：", type(exc).__name__)
        print(f"  断开，已读 {got_lines} 行")

        s0 = snapshot(sid)
        print(f"  断开瞬间 DB：{s0}")

        # ---- 2) 等 25 秒，看内容还涨不涨 ----
        for t in (8, 16, 25):
            await asyncio.sleep(t - (0 if t == 8 else (8 if t == 16 else 16)))
            s = snapshot(sid)
            print(f"  t={t:2d}s  DB：{s}")

        # ---- 3) 断开后重新发一句，看会不会被租约拦住 ----
        print("\n断开后重新发一条短消息，看能否正常开始……")
        try:
            async with c.stream("POST", f"/api/sessions/{sid}/messages",
                                json={"text": "只回答 OK"},
                                headers={**h, "Accept": "text/event-stream"},
                                timeout=120) as r:
                txt = ""
                async for line in r.aiter_lines():
                    if line.startswith("data:"):
                        txt += line[5:]
                print(f"  第二条返回了 {len(txt)} 字符")
                blocked = "generation_active" in txt or "generation" in txt.lower()
                print(f"  被租约拦住 = {blocked}")
        except Exception as exc:  # noqa: BLE001
            print("  第二条失败：", type(exc).__name__, str(exc)[:100])
            blocked = True

        final = snapshot(sid)
        print(f"\n最终 DB：{final}")
        asst = [x for x in final if x[0] == "assistant"]
        grew = bool(asst) and asst[0][2] > (s0[0][2] if s0 and s0[0][0] == "assistant" else 0)
        print()
        print("=" * 62)
        print(f"断开后 assistant 内容是否继续增长：{grew}")
        print(f"断开后能否立即重发：{'否' if blocked else '是'}")
        print("=" * 62)
        print("判读：")
        print("  增长=True  → 生成仍在后台跑，SSE 续传是纯 UI 问题")
        print("  增长=False → **刷新会掐死生成**，这是实质损失，应优先修")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
