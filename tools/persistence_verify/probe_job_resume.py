"""阶段 1 真机验证：断开 SSE 后生成是否继续到自然结束（真实湛卢）。

## 判据（与改之前对比）

改之前实测（`docs/20261115-…-持久化第三步.md` §1）：
```text
请求 6000 字 → 断开后只产出 73 字，句子写在一半，status=stopped
```

改之后应该：
  1. 断开后 content 持续增长（而不是停在断开那一刻）；
  2. 终态是 `complete`（而不是 `stopped`）；
  3. 正文长度接近请求量，末句完整。

另外验两条：
  - 断开后 `/stop` 仍能停下（停止按钮没被弄坏）；
  - 无订阅者期间盘上内容持续增长（`_persist` 不受订阅者影响）。

跑法：cd backend && python ../tools/persistence_verify/probe_job_resume.py
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
# 库路径可被环境变量覆盖：探针要能在隔离库上跑，否则默认指向生产库。
DB = Path(os.environ.get("HLZ_PROBE_DB") or (ROOT / "backend" / "data" / "app.db"))


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


async def new_session(c, tag: str):
    from tests.e2e.harness import ensure_user, ensure_zhanlu
    tok = await ensure_user(c, f"job_{tag}_{RID}".replace("-", "_"))
    await ensure_zhanlu(c, tok)
    h = {"Authorization": f"Bearer {tok}"}
    sid = (await c.post("/api/sessions", json={"title": f"JOB-{tag}-{RID}"},
                        headers=h)).json()["id"]
    return h, sid


async def case_a_disconnect_survives(c) -> bool:
    print("\n" + "=" * 68)
    print("A. 断开后生成继续到自然结束")
    print("=" * 68)
    h, sid = await new_session(c, "A")
    print(f"session = {sid}")

    prompt = ("请写一篇约 6000 字的长文，主题是「状态机与事件总线的测试难度对比」。"
              "分成 20 个小节，每节都要有标题和完整段落。不要使用任何工具。")

    got, cur = 0, ""
    async with c.stream("POST", f"/api/sessions/{sid}/messages",
                        json={"text": prompt},
                        headers={**h, "Accept": "text/event-stream"},
                        timeout=900) as r:
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            if line.startswith("event:"):
                cur = line[6:].strip()
            elif line.startswith("data:") and cur == "text_delta":
                got += len(line[5:])
                if got >= 200:
                    break
    st0, n0, _ = snap(sid)
    print(f"  断开时 text_delta≈{got}；DB status={st0} len={n0}")

    t0 = time.monotonic()
    samples = []
    for _ in range(60):                      # 最多 120 秒
        await asyncio.sleep(2)
        st, n, txt = snap(sid)
        el = time.monotonic() - t0
        samples.append((el, st, n))
        if st != "streaming":
            break
    st, n, txt = snap(sid)
    el = time.monotonic() - t0
    print(f"  最终：status={st} len={n}  （断开后 {el:.0f}s）")
    print(f"  末尾：…{txt[-70:]!r}")

    ok_grew = n > n0 + 500
    ok_complete = st == "complete"
    print(f"  [{'PASS' if ok_grew else 'FAIL'}] 断开后内容继续增长（{n0} → {n}）")
    print(f"  [{'PASS' if ok_complete else 'FAIL'}] 终态为 complete（实际 {st}）")
    return ok_grew and ok_complete


async def case_b_disconnected_persist(c) -> bool:
    print("\n" + "=" * 68)
    print("B. 无订阅者期间盘上内容持续增长（_persist 与订阅者无关）")
    print("=" * 68)
    h, sid = await new_session(c, "B")
    print(f"session = {sid}")

    got, cur = 0, ""
    async with c.stream("POST", f"/api/sessions/{sid}/messages",
                        json={"text": "写一篇 4000 字长文讲分布式事务。不要用工具。"},
                        headers={**h, "Accept": "text/event-stream"},
                        timeout=900) as r:
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            if line.startswith("event:"):
                cur = line[6:].strip()
            elif line.startswith("data:") and cur == "text_delta":
                got += len(line[5:])
                if got >= 100:
                    break
    st0, n0, _ = snap(sid)
    print(f"  断开时 len={n0}")
    marks = [n0]
    for _ in range(8):                       # 16 秒内多次采样
        await asyncio.sleep(2)
        _, n, _ = snap(sid)
        marks.append(n)
    grew_steps = sum(1 for a, b in zip(marks, marks[1:]) if b > a)
    print(f"  16 秒内长度采样：{marks}")
    ok = grew_steps >= 3
    print(f"  [{'PASS' if ok else 'FAIL'}] 无人订阅时仍持续落库（{grew_steps}/7 次采样有增长）")
    return ok


async def case_c_stop_still_works(c) -> bool:
    print("\n" + "=" * 68)
    print("C. 断开后 /stop 仍能停下（停止按钮没被解耦弄坏）")
    print("=" * 68)
    h, sid = await new_session(c, "C")
    print(f"session = {sid}")

    got, cur = 0, ""
    async with c.stream("POST", f"/api/sessions/{sid}/messages",
                        json={"text": "写一篇 8000 字长文讲编译原理。不要用工具。"},
                        headers={**h, "Accept": "text/event-stream"},
                        timeout=900) as r:
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            if line.startswith("event:"):
                cur = line[6:].strip()
            elif line.startswith("data:") and cur == "text_delta":
                got += len(line[5:])
                if got >= 100:
                    break
    st0, n0, _ = snap(sid)
    print(f"  断开时 status={st0} len={n0}")

    await asyncio.sleep(3)
    st1, n1, _ = snap(sid)
    print(f"  3 秒后（仍在跑）：status={st1} len={n1}")

    r = await c.post(f"/api/sessions/{sid}/stop", json={}, headers=h, timeout=60)
    print(f"  /stop → HTTP {r.status_code}")
    await asyncio.sleep(3)
    st2, n2, _ = snap(sid)
    print(f"  停止后：status={st2} len={n2}")

    ok = st2 == "stopped"
    ok_kept = n2 >= n1 - 50      # 已产出内容不该被抹掉
    print(f"  [{'PASS' if ok else 'FAIL'}] 停下后状态 stopped（实际 {st2}）")
    print(f"  [{'PASS' if ok_kept else 'FAIL'}] 已产出内容保留（{n1} → {n2}）")
    return ok and ok_kept


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        results = {}
        for name, fn in (("A", case_a_disconnect_survives),
                         ("B", case_b_disconnected_persist),
                         ("C", case_c_stop_still_works)):
            try:
                results[name] = await fn(c)
            except Exception as exc:  # noqa: BLE001
                print(f"  [{name}] 异常：{type(exc).__name__}: {exc}")
                results[name] = False

        print("\n" + "=" * 68)
        print(f"结果：{results}")
        print("=" * 68)
        return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
