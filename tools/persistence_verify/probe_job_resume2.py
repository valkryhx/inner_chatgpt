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


async def read_until(c, url, h, *, want_events, min_bytes=0, timeout=120,
                     stop_on_sync=False):
    """读到指定数量的正文，返回 (events_seen, text_bytes, sync_payloads, last_seq)。

    `last_seq` 取「本次连接收到的最后一个 `id:`」。

    **踩过的坑（2026-11-15）**：早期版本一满足「事件齐了 + 正文够了」就立刻
    `break`，而 SSE 里 `id:` 行通常在 `data:` **之后**，此刻还没读到，
    于是 `last_seq` 停在 0，R1 误报「seq 没前进（6 → 0）」—— 正文明明接上了
    203 字。修法不是去补读（对 httpx 的 `aiter_lines()` 再取 `__anext__()`
    会新建迭代器、不接续），而是**改成「先记够正文，再多收一个 id 才退出」**。
    """
    events: list[str] = []
    text = 0
    syncs: list[dict] = []
    last_seq = 0
    cur = ""
    t0 = time.monotonic()
    enough = False          # 正文 / 事件已达标
    enough_at = 0.0         # 达标时刻（用于兜底退出）
    id_after_enough = False  # 达标之后又读到一个 id
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
                    if enough:
                        id_after_enough = True
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
                    # R2 只关心「有没有降级 + 载荷形状」，拿到一帧就走，
                    # 不必把后续正文全读完。
                    if stop_on_sync:
                        break
                elif cur == "done":
                    break
            if want_events and all(e in events for e in want_events) and text >= min_bytes:
                if not enough:
                    enough = True
                    enough_at = time.monotonic()
                # 拿到达标之后的 id → last_seq 是最新的，可以走。
                # 兜底：达标后 3 秒仍无新 id（对端不再产帧）也走，
                # 否则会一直挂到 timeout，把 90s 的预算耗光。
                if id_after_enough or (time.monotonic() - enough_at) > 3.0:
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
    print("  ⚠️ 这条要有两个前提同时成立：")
    print("     (a) 重放缓冲被压小（HLZ_TURNJOB_REPLAY_FRAMES=20），否则挤不爆；")
    print("     (b) 续看时 **job 还活着** —— job 结束即摘表，buffer 随之消失，")
    print("         /stream 直接回 done，根本走不到 sync 分支。")
    print("     2026-11-15 实测：只满足 (a) 时仍报 SKIP，因为 4000 字那轮 6 秒就写完了。")
    h, sid = await new_session(c, "R2")
    print(f"session = {sid}")

    got, cur = 0, ""
    async with c.stream("POST", f"/api/sessions/{sid}/messages",
                        json={"text": "写一篇 12000 字长文讲分布式锁，分 30 小节。不要用工具。"},
                        headers={**h, "Accept": "text/event-stream"},
                        timeout=600) as r:
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            if line.startswith("event:"):
                cur = line[6:].strip()
            elif line.startswith("data:") and cur == "text_delta":
                got += len(line[5:])
                # 读到够多字就断开 —— 此时服务端已产出远超 20 帧（压小的缓冲
                # 上限），`after=0` 必然追不上。
                # **别 sleep**：等 2 秒都可能让整轮写完（实测 8000 上 2 秒就
                # 出了 5699 字 → job 摘表 → 只能 SKIP）。一断就重连最稳。
                if got >= 400:
                    break

    st, n = last_content(sid)
    print(f"  断开时：已收 {got} 字，库里 status={st} len={n}")
    if st != "streaming":
        print("  [SKIP] 生成已经结束，job 已摘表，sync 分支无从触发。")
        print("         （这条只在「续看时 job 还活着」时才验得到）")
        return "SKIP"

    events, text, syncs, seq, code = await read_until(
        c, f"/api/sessions/{sid}/stream?after=0", h,
        want_events=None, min_bytes=0, timeout=45, stop_on_sync=True)
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
        # 仍未溢出：**这条没验到**，但不是产品失败。
        print("  [SKIP] 缓冲未溢出，sync 分支未被触发 —— 这条没验到。")
        print("         前提：HLZ_TURNJOB_REPLAY_FRAMES=20 且 job 仍在运行。")
        return "SKIP"
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
        passed = [k for k, v in results.items() if v is True]
        skipped = [k for k, v in results.items() if v == "SKIP"]
        failed = [k for k, v in results.items() if v is False]
        print("\n" + "=" * 68)
        print(f"结果：{results}")
        print(f"  通过 {len(passed)}：{passed}")
        if skipped:
            print(f"  跳过 {len(skipped)}：{skipped}（环境不满足，不是失败）")
        if failed:
            print(f"  失败 {len(failed)}：{failed}")
        print("=" * 68)
        return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
