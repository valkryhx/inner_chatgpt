"""精确版：断开后生成到底停没停？（只读产品代码）

上一版探针有歧义：模型在 code_run 里睡着，25 秒内没产出任何文本，
所以「内容不增长」既可能是「停了」也可能是「还没开始写」。

这一版换构造：**不用工具**，让模型直接输出一段长文本，读几行就断开，
然后看 content 长度是否继续增长。这样「增长」与否唯一对应「生成是否继续」。

跑法：cd backend && python ../tools/persistence_verify/probe_disconnect2.py
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


def snap(sid: str) -> tuple[str, str, int]:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT role, status, length(content) n FROM messages "
        "WHERE session_id=? AND role='assistant' ORDER BY rowid", (sid,)).fetchall()
    con.close()
    if not rows:
        return ("?", "?", 0)
    r = rows[-1]
    return (r["role"], r["status"], r["n"])


async def main() -> int:
    from tests.e2e.harness import ensure_user, ensure_zhanlu

    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        tok = await ensure_user(c, f"d2_{RID}".replace("-", "_"))
        await ensure_zhanlu(c, tok)
        h = {"Authorization": f"Bearer {tok}"}
        sid = (await c.post("/api/sessions", json={"title": f"D2-{RID}"},
                            headers=h)).json()["id"]
        print(f"session = {sid}")

        # 明确要求长输出、不用工具，且分段（模型会流式吐字）
        prompt = ("请写一段约 1200 字的技术说明，主题是「为什么分布式系统里"
                  "状态机比事件总线更难测」。不要使用任何工具，直接输出文字。")

        print("发出后等 text_delta 累计 ≥200 字，再断开……")
        got_text = 0
        n = 0
        evs: list[str] = []
        cur = ""
        try:
            async with c.stream("POST", f"/api/sessions/{sid}/messages",
                                json={"text": prompt},
                                headers={**h, "Accept": "text/event-stream"},
                                timeout=300) as r:
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    n += 1
                    if line.startswith("event:"):
                        cur = line[6:].strip()
                        if cur not in evs:
                            evs.append(cur)
                    elif line.startswith("data:") and cur == "text_delta":
                        got_text += len(line[5:])
                        if got_text >= 200:
                            break
        except Exception as exc:  # noqa: BLE001
            print("  断开：", type(exc).__name__)
        print(f"  断开时 {n} 行 / text_delta 约 {got_text} 字符，事件序列：{evs[:12]}")

        samples = []
        prev = snap(sid)
        print(f"  断开瞬间：status={prev[1]} len={prev[2]}")
        samples.append(("断开瞬间", prev))
        for wait in (5, 10, 20, 35):
            await asyncio.sleep(wait if wait == 5 else (5 if wait == 10 else (10 if wait == 20 else 15)))
            cur = snap(sid)
            samples.append((f"+{wait}s", cur))
            print(f"  +{wait:2d}s：status={cur[1]} len={cur[2]}")

        grew = any(s[1][2] > prev[2] for s in samples[1:])
        final = samples[-1][1]
        print()
        print("=" * 62)
        print(f"最终长度 {final[2]}（断开时 {prev[2]}），状态 {final[1]}")
        print(f"断开后内容继续增长：{grew}")
        print("=" * 62)
        if grew:
            print("→ 生成在后台继续跑。断线只影响「看」，是纯 UI 问题。")
        else:
            print("→ **断开即掐死生成**，已产出的部分也停在那里。是实质损失。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
