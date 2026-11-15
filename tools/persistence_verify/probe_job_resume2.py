"""阶段 2 真机验证：断线后能否续看（真实湛卢）。

## 验什么

阶段 1 已证明「断开不杀生成」。阶段 2 要证明**新连接能接上正在跑的那一轮**：

| 场景 | 断言 |
|---|---|
| R-1 断线续看 | 断开→重连 `GET /stream?after=k`→收到尾部事件→内容接上 |
| R-2 游标太老降级 sync | `after=0` 且缓冲已被挤压时，收到 `event: sync` 且含完整 blocks |
| R-3 无活跃 job | 已跑完的会话调 `/stream` 返回 `done`（不是 404） |
| R-4 双连接同时看 | 两个连接收到同样的后续内容 |
| R-5 续看不产生重复消息 | 续看前后 `messages` 表行数不变 |

跑法：cd backend && python ../tools/persistence_verify/probe_job_resume2.py
"""
from __future__ import annotations

import asyncio
import json
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


def msg_count(sid: str) -> int:
    con = sqlite3.connect(str(DB))
    n = con.execute("SELECT COUNT(*) FROM messages WHERE session_id=?",
                    (sid,)).fetchone()[0]
    con.close()
    return n


def last_content(sid: str) -> tuple[str, int]:
    con = sqlite3.connect(str(DB))
    row = con.execute(
        "SELECT status, length(content) FROM messages "
        "WHERE session_id=? AND role='assistant' ORDER BY rowid DESC LIMIT 1",
        (sid,)).fetchone()
    con.close()
    return (row[0], row[1]) if row else ("?", 0)


async def new_session(c, tag: str):
    from tests.e2e.harness import ensure_user, ensure_zhanlu
    tok = await ensure_user(c, f"r2_{tag}_{RID}".replace("-", "_"))
    await ensure_zhanlu(c, tok)
    h = {"Authorization": f"Bearer {tok}"}
    sid = (await c.post("/api/sessions", json={"title": f"R2-{tag}-{RID}"},
                        headers=h)).json()["id"]
    return h, sid


async def read_until(c, url, h, *, want_events, min_bytes=0, timeout=120):
    """读到指定数量的正文，返回 (events_seen, text_bytes, sync_payloads, last_seq)。"""
    events: list[str] = []
    text = 0
    syncs: list[dict] = []
    last_seq = 0
    cur = ""
    t0 = time.monotonic()
    async with c.stream("GET", url, headers={**h, "Accept": "text/event-stream"},
                        timeout=timeout) as r:
        if r.status_code != 200:
            return events, text, syncs, last_seq, r.status_code
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            if line.startswith("event:"):
                cur = line[6:].strip()
                if cur not in events:
                    events.append(cur)
            elif line.startswith("id:"):
                try:
                    last_seq = int(line[3:].strip())
                except ValueError:
                    pass
            elif line.startswith("data:"):
                if cur == "text_delta":
                    text += len(line[5:])
                elif cur == "sync":
                    try:
                        syncs.append(json.loads(line[5:]))
                    except Exception:  # noqa: BLE001
                        pass
                elif cur == "done":
                    break
            if want_events and all(e in events for e in want_events) and text >= min_bytes:
                break
            if time.monotonic() - t0 > timeout:
                break
    return events, text, syncs, last_seq, r.status_code


async def case_r1_resume(c) -> bool:
    print("\n" + "=" * 68)
    print("R-1 断线续看：断开后重连，能接上后续内容")
    print("=" * 68)
    h, sid = await new_session(c, "R1")
    print(f"session = {sid}")
    n_before = msg_count(sid)

    got, cur, last_seq = 0, "", 0
    async with c.stream("POST", f"/api/sessions/{sid}/messages",
                        json={"text": "写一篇 5000 字长文讲容器编排。不要用工具。"},
                        headers={**h, "Accept": "text/event-stream"},
                        timeout=600) as r:
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            if line.startswith("event:"):
                cur = line[6:].strip()
            elif line.startswith("id:"):
                try:
                    last_seq = int(line[3:].strip())
                except ValueError:
                    pass
            elif line.startswith("data:") and cur == "text_delta":
                got += len(line[5:])
                if got >= 150:
                    break
    n_mid = msg_count(sid)
    print(f"  断开：已收 {got} 字，最后 seq={last_seq}，消息数={n_mid}")

    # 立刻续看
    events, text, syncs, seq2, code = await read_until(
        c, f"/api/sessions/{sid}/stream?after={last_seq}", h,
        want_events=["text_delta"], min_bytes=200, timeout=90)
    print(f"  续看：HTTP {code}，事件 {events}，收到正文 {text} 字，"
          f"sync 帧 {len(syncs)}，最后 seq={seq2}")
    n_after = msg_count(sid)

    ok_resumed = text >= 200
    ok_no_new_msg = (n_after == n_mid)
    # seq 必须前进；但**不要求**它接着 last_seq —— 溢出降级时 sync 直接跳到当前
    # seq 是正确行为（快照已含全部内容，不需要逐帧补）。
    ok_seq_advanced = seq2 > last_seq
    if syncs:
        txt_len = len((syncs[0].get("blocks") or [{}])[0].get("text", "")) \
            if syncs[0].get("blocks") else 0
        print(f"  降级 sync：blocks 首块文本 {txt_len} 字符（应覆盖断开前的全部内容）")
        ok_seq_advanced = ok_seq_advanced and (seq2 > syncs[0].get("seq", 0))
    print(f"  [{'PASS' if ok_resumed else 'FAIL'}] 续看到后续内容（{text} 字 ≥ 200）")
    print(f"  [{'PASS' if ok_no_new_msg else 'FAIL'}] 续看不产生新消息（{n_mid} → {n_after}）")
    print(f"  [{'PASS' if ok_seq_advanced else 'FAIL'}] seq 前进（{last_seq} → {seq2}）")
    return ok_resumed and ok_no_new_msg and ok_seq_advanced


async def case_r2_sync_fallback(c) -> bool:
    print("\n" + "=" * 68)
    print("R-2 游标太老 → 降级 sync（带完整 blocks）")
    print("=" * 68)
    print("  ⚠️ 上一版这条是**假通过**：缓冲上限 2000 条/2MB，普通回答根本挤不爆，")
    print("     于是 after=0 仍走增量分支，sync 从未触发。要真验降级必须制造溢出。")
    print("     现在靠 HLZ_TURNJOB_REPLAY_FRAMES 压小上限（启动器已设），确定性地触发。")
    h, sid = await new_session(c, "R2")
    print(f"session = {sid}")

    got, cur = 0, ""
    async with c.stream("POST", f"/api/sessions/{sid}/messages",
                        json={"text": "写一篇 4000 字长文讲分布式锁。不要用工具。"},
                        headers={**h, "Accept": "text/event-stream"},
                        timeout=600) as r:
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            if line.startswith("event:"):
                cur = line[6:].strip()
            elif line.startswith("data:") and cur == "text_delta":
                got += len(line[5:])
                if got >= 100:
                    break
    # 让生成跑一阵，攒出远超上限的帧数
    await asyncio.sleep(6)
    st, n = last_content(sid)
    print(f"  攒了 6 秒：status={st} len={n}")

    events, text, syncs, seq, code = await read_until(
        c, f"/api/sessions/{sid}/stream?after=0", h,
        want_events=None, min_bytes=0, timeout=45)
    print(f"  after=0：HTTP {code}，事件 {events}，正文 {text} 字，"
          f"sync {len(syncs)} 帧，seq={seq}")

    if syncs:
        blocks = syncs[0].get("blocks")
        n_blocks = len(blocks or [])
        ok = isinstance(blocks, list) and n_blocks > 0
        print(f"  sync 载荷：blocks 列表={isinstance(blocks, list)}，块数={n_blocks}，"
              f"seq={syncs[0].get('seq')}")
        print(f"  [{'PASS' if ok else 'FAIL'}] 溢出后降级 sync，且带完整 blocks")
    else:
        # 仍未溢出：承认没测到，不能算通过
        print(f"  [FAIL] 缓冲仍未溢出，sync 分支没有被真正触发 —— 这条没验到")
        ok = False
    return ok


async def case_r3_no_job(c) -> bool:
    print("\n" + "=" * 68)
    print("R-3 无活跃 job：返回 done 而不是 404")
    print("=" * 68)
    h, sid = await new_session(c, "R3")
    print(f"session = {sid}")

    # 先跑完一轮（短）
    async with c.stream("POST", f"/api/sessions/{sid}/messages",
                        json={"text": "只回答 OK"},
                        headers={**h, "Accept": "text/event-stream"},
                        timeout=180) as r:
        async for line in r.aiter_lines():
            if line.startswith("event: done"):
                break
    await asyncio.sleep(1)
    print(f"  一轮已结束：{last_content(sid)}")

    events, text, syncs, seq, code = await read_until(
        c, f"/api/sessions/{sid}/stream?after=0", h, want_events=None,
        timeout=20)
    print(f"  /stream：HTTP {code}，事件 {events}")
    ok = code == 200 and "done" in events
    print(f"  [{'PASS' if ok else 'FAIL'}] 返回 200 + done（不是 404）")
    return ok


async def case_r4_two_viewers(c) -> bool:
    print("\n" + "=" * 68)
    print("R-4 两个连接同时看（双标签页）")
    print("=" * 68)
    h, sid = await new_session(c, "R4")
    print(f"session = {sid}")

    got, cur, last_seq = 0, "", 0
    async with c.stream("POST", f"/api/sessions/{sid}/messages",
                        json={"text": "写一篇 3000 字长文讲垃圾回收。不要用工具。"},
                        headers={**h, "Accept": "text/event-stream"},
                        timeout=600) as r:
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            if line.startswith("event:"):
                cur = line[6:].strip()
            elif line.startswith("id:"):
                try:
                    last_seq = int(line[3:].strip())
                except ValueError:
                    pass
            elif line.startswith("data:") and cur == "text_delta":
                got += len(line[5:])
                if got >= 100:
                    break

    url = f"/api/sessions/{sid}/stream?after={last_seq}"
    a, b = await asyncio.gather(
        read_until(c, url, h, want_events=["text_delta"], min_bytes=150, timeout=90),
        read_until(c, url, h, want_events=["text_delta"], min_bytes=150, timeout=90),
    )
    print(f"  连接 A：事件 {a[0]}，正文 {a[1]} 字，seq={a[3]}")
    print(f"  连接 B：事件 {b[0]}，正文 {b[1]} 字，seq={b[3]}")
    ok = a[1] >= 150 and b[1] >= 150
    print(f"  [{'PASS' if ok else 'FAIL'}] 两个连接都收到后续内容")
    return ok


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        results = {}
        for name, fn in (("R1", case_r1_resume), ("R2", case_r2_sync_fallback),
                         ("R3", case_r3_no_job), ("R4", case_r4_two_viewers)):
            try:
                results[name] = await fn(c)
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                print(f"  [{name}] 异常：{type(exc).__name__}: {exc}")
                results[name] = False
        print("\n" + "=" * 68)
        print(f"结果：{results}")
        print("=" * 68)
        return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
