"""断开后能不能立刻重发？（看 HTTP 状态码，不猜文本）

早期探针用「响应文本里有没有 generation 这个词」判断被租约拦住 ——
那个判据是坏的：正常回复里也可能出现该词，而 409 的 body 里必然出现。
所以这一版直接看**状态码**。

要验的：
  Q1 断开后立刻重发 → 200 还是 409？
  Q2 若 409，等多久能恢复？
  Q3 那条被掐断的 assistant 消息最终是什么状态？

跑法：python tools/persistence_verify/probe_disconnect.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402

# 端口可被环境变量覆盖：探针常在隔离后端（非 8000）上跑。
BASE = os.environ.get("HLZ_PROBE_BASE") or "http://127.0.0.1:8000"
RID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"
# 库路径随 BASE 一起由环境变量给出：探针要能在隔离库上跑，
# 否则默认指向生产库会让每次验证都往真实数据里写会话。
DB = Path(os.environ.get("HLZ_PROBE_DB") or (ROOT / "backend" / "data" / "app.db"))


def rows(sid: str) -> list[tuple[str, str, int]]:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rs = con.execute(
        "SELECT role, status, length(content) n FROM messages "
        "WHERE session_id=? ORDER BY rowid", (sid,)).fetchall()
    con.close()
    return [(r["role"], r["status"], r["n"]) for r in rs]


async def try_send(c, sid, h, text) -> tuple[int, str]:
    """发一条消息，只取状态码（读完前 1 个事件就关）。"""
    try:
        async with c.stream("POST", f"/api/sessions/{sid}/messages",
                            json={"text": text},
                            headers={**h, "Accept": "text/event-stream"},
                            timeout=120) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode("utf-8", "replace")
                return r.status_code, body[:200]
            got = 0
            async for line in r.aiter_lines():
                if line.strip():
                    got += 1
                    if got >= 2:
                        break
            return 200, ""
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {str(exc)[:120]}"


async def main() -> int:
    from tests.e2e.harness import ensure_user, ensure_zhanlu

    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        tok = await ensure_user(c, f"d4_{RID}".replace("-", "_"))
        await ensure_zhanlu(c, tok)
        h = {"Authorization": f"Bearer {tok}"}
        sid = (await c.post("/api/sessions", json={"title": f"D4-{RID}"},
                            headers=h)).json()["id"]
        print(f"session = {sid}")

        # 先起一轮长生成，收到正文就断开
        cur, got = "", 0
        async with c.stream("POST", f"/api/sessions/{sid}/messages",
                            json={"text": "写一篇 6000 字长文，讲状态机与事件总线的测试难度，"
                                         "分 20 小节。不要用工具。"},
                            headers={**h, "Accept": "text/event-stream"},
                            timeout=600) as r:
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                if line.startswith("event:"):
                    cur = line[6:].strip()
                elif line.startswith("data:") and cur == "text_delta":
                    got += len(line[5:])
                    if got >= 150:
                        break
        print(f"断开（已收 {got} 字）")

        # Q1：立刻重发
        code, body = await try_send(c, sid, h, "只回答 OK")
        print(f"\nQ1 断开后立刻重发 → HTTP {code}  {body[:120]}")
        if code == 409:
            for i in range(1, 7):
                await asyncio.sleep(1)
                code, body = await try_send(c, sid, h, "只回答 OK")
                print(f"    +{i}s 重试 → HTTP {code} {body[:80]}")
                if code == 200:
                    print(f"   → 约 {i}s 后租约自愈")
                    break

        # Q3：**等生成真正结束**再看。只 sleep 2s 查库会读到 status=streaming，
        # 那只是「这一刻还在写」，不是「卡住了」——2026-11-15 实测：
        # 2 秒时 status=streaming / len=2146，最终 status=complete / len=7535。
        # 判据错会让人误判成「断线导致消息永远卡在 streaming」。
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            asst_now = [x for x in rows(sid) if x[0] == "assistant"]
            if asst_now and asst_now[0][1] != "streaming":
                break
            await asyncio.sleep(3)
        print()
        print(f"Q3 消息表：{rows(sid)}")
        asst = [x for x in rows(sid) if x[0] == "assistant"]
        if asst:
            _role, status, n = asst[0]
            print(f"   被掐断的那条：status={status} len={n}")
            if status == "streaming":
                print("   ⚠ 等了 300s 仍是 streaming —— 这才是真卡住的信号")
            else:
                print(f"   ✓ 断开不影响生成：最终 status={status}（内容继续写完了）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
