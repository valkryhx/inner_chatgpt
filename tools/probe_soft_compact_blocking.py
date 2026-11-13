"""探针 B：验证「会话超软线时，首帧之前会不会先跑一次 LLM 摘要」。

这是抓到「首帧被推迟 5.1 秒」的那个探针。
见 docs/20261112-Hlaudz首帧串行阻塞核实与优化设计.md §1.1。

做法：对照组 = 空历史新会话；实验组 = 先灌入远超 soft_limit 的历史。
对比两组的 `message_start` 时刻，并确认实验组流里出现 `compaction` 事件。

用法（后端需在跑）：
    python tools/probe_soft_compact_blocking.py
    HLZ_UID=<user_id> python tools/probe_soft_compact_blocking.py

⚠️ 注意：实验组会往 `session_history` 灌入大量假历史（约 120 万字符）到一个**新建会话**，
不会碰你已有的会话。跑完可以在界面上删掉那个 `perf-heavy` 会话。
"""
import asyncio
import json
import os
import sys
import time
import uuid

import httpx

BASE = os.environ.get("HLZ_BASE", "http://127.0.0.1:8000")
UID = os.environ.get("HLZ_UID", "")
TOKEN = os.environ.get("HLZ_TOKEN", "")


async def probe(headers: dict, sid: str, label: str) -> dict[str, float] | None:
    t0 = time.monotonic()
    marks: list[tuple[float, str]] = []
    payload = {"text": "只回答两个字：收到", "generation_id": uuid.uuid4().hex}
    async with httpx.AsyncClient(base_url=BASE, timeout=300.0) as c:
        async with c.stream("POST", f"/api/sessions/{sid}/messages", json=payload,
                            headers=headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                print(f"{label}: HTTP {resp.status_code} {body[:200]}")
                return None
            buf = b""
            async for chunk in resp.aiter_bytes():
                buf += chunk
                while b"\n\n" in buf:
                    frame, buf = buf.split(b"\n\n", 1)
                    name = ""
                    for line in frame.split(b"\n"):
                        if line.startswith(b"event:"):
                            name = line[6:].strip().decode()
                    if name:
                        marks.append(((time.monotonic() - t0) * 1000, name))
                    if name == "done":
                        break
                if marks and marks[-1][1] == "done":
                    break
    first: dict[str, float] = {}
    for t, n in marks:
        first.setdefault(n, t)
    print(f"{label}:")
    for k in ("compaction", "message_start", "team_event", "text_delta", "done"):
        if k in first:
            print(f"    {first[k]:9.1f} ms  {k}")
    return first


async def main() -> int:
    sys.path.insert(0, ".")
    from backend.app import auth, db

    uid = UID
    if not uid:
        print("需要 HLZ_UID（用户 id），或设 HLZ_EMAIL/HLZ_PASSWORD 走登录")
        return 1

    if TOKEN:
        headers = {"Authorization": f"Bearer {TOKEN}"}
    else:
        u = await db.get_user(uid)
        token, _ = auth._make_token(u["id"], int(u.get("token_ver", 0)), "access",
                                    auth.ACCESS_TTL)
        headers = {"Authorization": f"Bearer {token}"}

    cfg = await db.get_default_llm_config(uid)
    if not cfg:
        print("该用户没有默认模型配置，无法测")
        return 1
    soft = int(cfg.get("auto_compact_tokens")
               or int(cfg.get("context_window", 128000) * 0.90))
    print(f"soft_limit = {soft} tokens")

    s1 = await db.create_session(uid, title="perf-empty")
    a = await probe(headers, s1["id"], "A 空历史（对照）")

    s2 = await db.create_session(uid, title="perf-heavy")
    filler = "这是一段用于占满上下文的测试文本，目的是让压缩闸门触发。" * 40
    hist = []
    for i in range(int(soft * 4 / 900) + 20):
        hist.append({"role": "user", "content": f"{i} {filler}"})
        hist.append({"role": "assistant", "content": f"收到 {i} {filler}"})
    from backend.app.history_store import get_history_store
    await get_history_store().set(uid, s2["id"], hist)
    print(f"灌入历史 {len(hist)} 条（约 {len(json.dumps(hist, ensure_ascii=False))} 字符）")

    b = await probe(headers, s2["id"], "B 超软线（实验）")

    if a and b and "message_start" in a and "message_start" in b:
        d = b["message_start"] - a["message_start"]
        print("\n结论:")
        print(f"  message_start: 空历史 {a['message_start']:.0f}ms -> "
              f"超软线 {b['message_start']:.0f}ms  (差 {d:+.0f}ms)")
        print(f"  超软线场景是否出现 compaction 事件: {'compaction' in b}")
        print(f"  判定: {'首帧前有 LLM 摘要阻塞（缺陷成立）' if d > 500 else '未观察到显著阻塞'}")
    print(f"\n清理提示：可在界面删除会话 {s1['id']} / {s2['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
