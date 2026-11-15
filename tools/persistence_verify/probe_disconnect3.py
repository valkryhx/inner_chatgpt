"""第三版：断开后到底是「跑完了」还是「跑到一半被掐」？

前两版的歧义都已排除，但仍有一个关键区分没做：

  +5s 时 content=1511 且此后不再增长、status=stopped。

两种解释都成立：
  H1 生成被掐断 —— 1511 字是一段**截断**的正文；
  H2 生成正常跑完 —— 但被记成 stopped（那只是状态标注问题）。

区分办法：**要求远超断开检测窗口能吐出的量**（例如 6000 字），
然后看最终长度是不是明显短于一个完整回答。同时打印末尾 80 字，
截断的正文一眼就能看出来（句子没写完）。

另外记录：从客户端关闭到 status 变化的时间（= 服务端断开检测延迟）。

跑法：cd backend && python ../tools/persistence_verify/probe_disconnect3.py
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


def snap(sid: str) -> tuple[str, int, str]:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT status, length(content) n, content FROM messages "
        "WHERE session_id=? AND role='assistant' ORDER BY rowid DESC LIMIT 1",
        (sid,)).fetchone()
    con.close()
    if row is None:
        return ("?", 0, "")
    return (row["status"], row["n"], row["content"] or "")


async def main() -> int:
    from tests.e2e.harness import ensure_user, ensure_zhanlu

    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        tok = await ensure_user(c, f"d3_{RID}".replace("-", "_"))
        await ensure_zhanlu(c, tok)
        h = {"Authorization": f"Bearer {tok}"}
        sid = (await c.post("/api/sessions", json={"title": f"D3-{RID}"},
                            headers=h)).json()["id"]
        print(f"session = {sid}")

        prompt = ("请写一篇约 6000 字的长文，主题是「状态机与事件总线的测试难度对比」。"
                  "分成 20 个小节，每节都要有标题和完整段落。"
                  "不要使用任何工具，直接输出文字。")

        print("等 text_delta 累计 ≥200 字后断开……")
        got = 0
        cur = ""
        try:
            async with c.stream("POST", f"/api/sessions/{sid}/messages",
                                json={"text": prompt},
                                headers={**h, "Accept": "text/event-stream"},
                                timeout=600) as r:
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    if line.startswith("event:"):
                        cur = line[6:].strip()
                    elif line.startswith("data:") and cur == "text_delta":
                        got += len(line[5:])
                        if got >= 200:
                            break
        except Exception as exc:  # noqa: BLE001
            print("  断开：", type(exc).__name__)
        t_close = time.monotonic()
        st0, n0, _ = snap(sid)
        print(f"  断开时 text_delta≈{got} 字；DB status={st0} len={n0}")

        # 细粒度采样，捕捉「什么时候停的」和「检测延迟」
        stopped_at = None
        last = (st0, n0)
        for i in range(1, 25):          # 最多 48 秒
            await asyncio.sleep(2)
            st, n, txt = snap(sid)
            el = time.monotonic() - t_close
            if (st, n) != last:
                print(f"  +{el:5.1f}s  status={st} len={n}   ← 变化")
                last = (st, n)
            if st != "streaming" and stopped_at is None:
                stopped_at = el
                break

        await asyncio.sleep(3)
        st, n, txt = snap(sid)
        print()
        print("=" * 64)
        print(f"最终：status={st}  len={n}")
        print(f"停止时刻（相对断开）：{'%.1fs' % stopped_at if stopped_at else '未在采样窗口内停'}")
        print(f"末尾 80 字：…{txt[-80:]!r}")
        print("=" * 64)
        # 6000 字的请求，能吐出的量远小于它 → 一定是被掐断
        if n < 3000:
            print(f"→ 正文只有 {n} 字（请求 6000 字），且末尾未收尾：**生成被掐断**")
        else:
            print(f"→ 正文 {n} 字，接近完整：可能只是状态标注问题")
        print(f"   （断开检测延迟约 {stopped_at:.1f}s，期间生成继续在跑）" if stopped_at else "")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
