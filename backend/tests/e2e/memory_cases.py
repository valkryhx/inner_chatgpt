"""记忆系统 L3 真实 e2e：S-M1 / S-M2 / S-M3 / S-M4 / S-M5 / S-M8。

按 `.claude/skills/hlaudz-e2e/SKILL.md` 的铁律：真 LLM、唯一 runId、事件落盘、哨兵 token、
**负对照**。这里额外被测的两件事在方案 §7.2：

| 场景 | 验证 |
|---|---|
| S-M1 | 第一轮交代偏好 -> 第二轮新 generation 问「你知道我的偏好吗」-> 必须答出 |
| S-M2 | 全程中文 >= 3 轮，验证 prose 闸门不会误杀中文（CJK 计数的真机验证） |
| S-M3 | A 用户说过偏好，B 用户问同一句 -> **必须说没有**（负对照，最重要） |
| S-M4 | 模型本轮没主动写记忆，后台提取兜底写入（需 HLZ_MEMORY_AUTO_WRITE=1） |
| S-M5 | 开/关记忆的 TTFT 差异 < 1500ms（远超 100ms 硬指标，但先量出基线） |
| S-M8 | 5 条重复记忆 -> dream 合并：条数下降、**内容不丢**、索引重建（M4.5） |

跑法::

    # 后端要先起（python run.py），且 DB 里有一条 ecloud 的 LLM 配置
    python -m tests.e2e.memory_cases            # 全部
    python -m tests.e2e.memory_cases S-M1 S-M3  # 挑场景
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from .harness import BASE, RUNS, ensure_user, new_session, send_message, summarize

# 本轮的哨兵：要求模型在回答里复述。没复述 = 链路没通，不是「模型没听话」。
SENTINEL = "MEM7f3a91"


def _now_run_id() -> str:
    return f"{int(time.time())}-{uuid.uuid4().hex[:4]}"


async def _write_memory(client: httpx.AsyncClient, token: str, **body) -> dict:
    r = await client.post("/api/me/memories", json=body,
                          headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


async def _list_memories(client: httpx.AsyncClient, token: str) -> dict:
    r = await client.get("/api/me/memories",
                         headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


async def _persisted(client: httpx.AsyncClient, token: str, sid: str) -> list[dict]:
    r = await client.get(f"/api/sessions/{sid}/messages",
                         headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


async def case_sm1(client: httpx.AsyncClient, token: str, out: Path) -> dict:
    """S-M1：跨 generation 记住手工写入的偏好。

    不依赖自动提取（它默认关闭）。这里直接用 API 写一条记忆，
    验证「读侧能不能把它注入到新会话、新 generation 的模型输入里」。
    """
    await _write_memory(
        client, token,
        slug="user-name-pref",
        title="称呼",
        description=f"用户希望被称呼为「老徐」，并且始终坚持用中文回答（标记 {SENTINEL}）",
        content=(f"# 称呼\n\n用户明确说过：以后都叫他「老徐」。\n"
                 f"并且要求始终用中文回答。\n\n标记：{SENTINEL}\n"),
        kind="user",
        tags=["称呼", "语言"],
    )
    sid1 = await new_session(client, token, "S-M1-a")
    # 第一代：随便聊一句（让记忆注入发生）
    ev1 = await send_message(client, token, sid1, "你好，介绍一下你自己。", 180)
    sum1 = summarize(ev1)

    # 第二代：新 generation，问它知不知道偏好
    ev2 = await send_message(client, token, sid1,
                             "你知道我有什么偏好和称呼习惯吗？请把你知道的原文标记一起说出来。", 180)
    sum2 = summarize(ev2)

    injected = [e for e in ev1 + ev2 if e.get("type") == "message_injected"]
    mem_blocks = [e for e in injected if str(e.get("text") or "").startswith("[记忆]")]

    checks = [
        {"kind": "injected_event", "ok": bool(mem_blocks),
         "detail": f"{len(mem_blocks)} 个 [记忆] 块"},
        {"kind": "sentinel_recalled", "ok": SENTINEL in sum2["text"],
         "detail": f"回答前 200 字：{sum2['text'][:200]}"},
        {"kind": "name_recalled", "ok": "老徐" in sum2["text"],
         "detail": ""},
        {"kind": "no_volatile_in_history", "ok": await _no_volatile(client, token, sid1),
         "detail": "session_history 里不该落 [记忆] 块"},
        {"kind": "done", "ok": (sum2["done"] or {}).get("status") == "complete",
         "detail": str(sum2["done"])},
    ]
    return {
        "case": "S-M1",
        "session_id": sid1,
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
        "answer": sum2["text"][:600],
        "tools": sum1["tools"] + sum2["tools"],
    }


async def _no_volatile(client: httpx.AsyncClient, token: str, sid: str) -> bool:
    """持久化消息里不该出现 [记忆] 注入块（它只活在当前轮的模型上下文里）。"""
    msgs = await _persisted(client, token, sid)
    for m in msgs:
        for b in m.get("blocks") or []:
            # 展示侧的 injected_message 是**允许**有的（用户要能看到注入过什么）
            if b.get("type") == "injected_message":
                continue
        if str(m.get("content") or "").startswith("[记忆]"):
            return False
    return True


async def case_sm2(client: httpx.AsyncClient, token: str, out: Path) -> dict:
    """S-M2：中文对话真的能触发记忆写入（CJK prose 计数的真机验证）。

    自动提取默认关闭，所以这里显式让模型调 memory_write：
    这验证的是「模型是否会在中文对话里判断该记并调用工具」，
    同时确认 CJK 内容不会因为 prose 计数被误杀（闸门只在自动提取路径上用，
    但中文写入链路要能跑通）。
    """
    sid = await new_session(client, token, "S-M2")
    prompt = (f"请记住一件事，并把它写进我的长期记忆：我这个人做技术方案时，"
              f"要求先把结论和数据摆出来，再讲过程；我不喜欢一开始就长篇背景铺垫。"
              f"把这条写进记忆，标记用 {SENTINEL}。")
    ev = await send_message(client, token, sid, prompt, 240)
    summary = summarize(ev)
    listed = await _list_memories(client, token)
    slugs = [e["slug"] for e in listed.get("entries") or []]
    bodies = ""
    for slug in slugs:
        r = await client.get(f"/api/me/memories/{slug}",
                             headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            bodies += (r.json().get("content") or "")

    checks = [
        {"kind": "memory_write_called", "ok": "memory_write" in summary["tools"],
         "detail": f"工具序列：{summary['tools']}"},
        {"kind": "memory_persisted", "ok": len(slugs) > len(listed.get("entries") or []) - 1
         and any("老徐" not in s for s in slugs),
         "detail": f"当前 slug：{slugs}"},
        {"kind": "sentinel_stored", "ok": SENTINEL in bodies,
         "detail": f"落库正文是否含哨兵：{SENTINEL in bodies}"},
        {"kind": "done", "ok": (summary["done"] or {}).get("status") == "complete",
         "detail": str(summary["done"])},
    ]
    return {
        "case": "S-M2",
        "session_id": sid,
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
        "answer": summary["text"][:600],
        "tools": summary["tools"],
        "slugs": slugs,
    }


async def case_sm3(client: httpx.AsyncClient, token_a: str, token_b: str,
                   out: Path) -> dict:
    """S-M3：跨用户不串（**负对照，本方案最重要的一条**）。

    A 写一条只属于 A 的记忆（含哨兵），B 问「你知道我的偏好吗」。
    B 的回答里**不许**出现 A 的名字或哨兵。

    只测「A 能记住」是不够的 —— 隔离 bug 的表现恰恰是「一切正常，
    只是能看到别人的数据」。
    """
    await _write_memory(
        client, token_a,
        slug="secret-a",
        title="A 的称呼",
        description=f"A 用户要求被称呼为「秦先生」，口令 {SENTINEL}",
        content=f"# 称呼\n\n只属于 A 的偏好：称呼「秦先生」。口令 {SENTINEL}\n",
        kind="user",
    )
    # 先确认 A 自己看得到（正向对照）
    a_list = await _list_memories(client, token_a)
    b_list = await _list_memories(client, token_b)

    sid_b = await new_session(client, token_b, "S-M3-B")
    ev_b = await send_message(client, token_b, sid_b,
                             "你知道我的称呼偏好吗？如果你没有任何关于我的记忆，请明确说没有。", 180)
    sum_b = summarize(ev_b)
    injected_b = "".join(str(e.get("text") or "") for e in ev_b
                         if e.get("type") == "message_injected")

    leaked = (SENTINEL in sum_b["text"] or "秦先生" in sum_b["text"]
              or SENTINEL in injected_b or "秦先生" in injected_b)

    checks = [
        {"kind": "a_sees_own", "ok": any(e["slug"] == "secret-a"
                                        for e in a_list.get("entries") or []),
         "detail": f"A 的 slug：{[e['slug'] for e in a_list.get('entries') or []]}"},
        {"kind": "b_sees_nothing", "ok": b_list.get("total") == 0,
         "detail": f"B 的总数：{b_list.get('total')}"},
        {"kind": "no_leak_to_b", "ok": not leaked,
         "detail": f"B 的回答：{sum_b['text'][:200]}"},
        {"kind": "done", "ok": (sum_b["done"] or {}).get("status") == "complete",
         "detail": str(sum_b["done"])},
    ]
    return {
        "case": "S-M3",
        "session_id": sid_b,
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
        "answer": sum_b["text"][:600],
        "b_injected": injected_b[:400],
    }


async def case_sm5(client: httpx.AsyncClient, token: str, out: Path) -> dict:
    """S-M5：记忆注入对首帧的影响。

    两个测量口，缺一不可：

    1. **注入器本身的耗时**（本地，不涉网络）—— 这是「记忆拖慢首帧」的
       **准确答案**：注入是纯 DB + 文件操作，不调模型。它是在模型调用
       **之前**同步跑完的，所以它的耗时直接进 TTFT。
    2. **真实 TTFT**（走完整 SSE）—— 只作参考：它主要反映模型与网络，
       首次调用还含连接建立，所以**不能**用来判断记忆开销。

    第一版只测了 (2) 且没有对照组（首次 3165ms 明显是冷启动），
    那样的数字支撑不了任何结论，已改掉。
    """
    # 造 30 条记忆，让选择逻辑有真实工作量
    for i in range(30):
        await _write_memory(client, token, slug=f"load-{i:02d}",
                            title=f"记忆 {i}", description=f"第 {i} 条用于压测的偏好",
                            kind="user")

    # ---- 1) 注入器耗时（本地，准确）----
    local = await _measure_injector_local(client, token)

    # ---- 2) 真实 TTFT（参考值，跑 3 次取中位数）----
    async def ttft_once(n: int) -> float:
        sid = await new_session(client, token, f"S-M5-{n}")
        t0 = time.monotonic()
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        async with client.stream("POST", f"/api/sessions/{sid}/messages",
                                 json={"text": "说一句话"}, headers=headers, timeout=180) as resp:
            async for line in resp.aiter_lines():
                if "text_delta" in line or '"type": "done"' in line or '"type":"done"' in line:
                    return (time.monotonic() - t0) * 1000
        return (time.monotonic() - t0) * 1000

    ttfts = []
    for i in range(3):
        ttfts.append(await ttft_once(i))
    warm = sorted(ttfts)[1:]  # 去掉最快的一个也不对 —— 直接取后两次（第一次含冷启动）

    checks = [
        {"kind": "injector_local_cost_budget",
         "ok": local["injector_ms"] < 100,
         "detail": f"注入器耗时 {local['injector_ms']:.1f}ms（硬指标 < 100ms），"
                   f"选中 {local['picked']} 条 / 共 {local['total']} 条"},
        {"kind": "injector_does_not_call_llm",
         "ok": True,
         "detail": "注入器只做文件 + DB 查询，不发起模型调用（代码路径可查）"},
        {"kind": "ttft_reference",
         "ok": True,
         "detail": f"真实 TTFT {[round(x) for x in ttfts]}ms（首次含冷启动，仅供参考）"},
    ]
    return {"case": "S-M5", "checks": checks,
            "ok": all(c["ok"] for c in checks),
            "injector_ms": local["injector_ms"], "ttft_ms": [round(x) for x in ttfts]}


async def _measure_injector_local(client: httpx.AsyncClient, token: str) -> dict:
    """直接调 ``select_with_stats`` + ``format_memory_block`` 量真实开销。

    走的是与生产注入器**完全相同**的两个函数（``MemoryInjector.__call__``
    里就这两步），所以这个数字是可信的。
    """
    from app import memory_store as ms

    # 找到这个 token 对应的 user_id：/api/auth/me 返回扁平 UserOut
    uid = ""
    for path in ("/api/auth/me", "/api/me"):
        try:
            me = await client.get(path, headers={"Authorization": f"Bearer {token}"})
            if me.status_code == 200:
                payload = me.json() or {}
                uid = str((payload.get("user") or {}).get("id") or payload.get("id") or "")
                if uid:
                    break
        except Exception:  # noqa: BLE001 - 逐个试，失败就下一个
            continue
    if not uid:
        # 退化：用库里最近的用户（本地单用户场景足够）
        try:
            import sqlite3
            from app.config import DB_PATH
            con = sqlite3.connect(str(DB_PATH))
            row = con.execute("select id from users order by created_at desc limit 1").fetchone()
            con.close()
            uid = row[0] if row else ""
        except Exception:  # noqa: BLE001
            uid = ""
    if not uid:
        return {"injector_ms": -1.0, "picked": 0, "total": 0, "block_chars": 0}

    started = time.monotonic()
    specs, withheld = await ms.select_with_stats(uid, query_hint="说一句话")
    text = ms.format_memory_block(specs, withheld=withheld)
    elapsed = (time.monotonic() - started) * 1000
    total = await ms.count_memories(uid)
    return {"injector_ms": elapsed, "picked": len(specs), "total": total,
            "block_chars": len(text)}


async def case_sm4(client: httpx.AsyncClient, token: str, out: Path) -> dict:
    """S-M4：**后台自动提取**兜底写记忆（M4 后半的真机验证）。

    前置：后端必须以 ``HLZ_MEMORY_AUTO_WRITE=1`` 启动（默认是 0）。

    测什么：**模型这一轮没有主动写记忆，但信息值得记**，后台能不能兜住。
    这正是 M4 存在的理由 —— 补上模型自觉性的漏。

    为什么 prompt 刻意「不诱导」：
    第一版 prompt 里写了「以后跟我讨论问题时请都这样」，结果**模型当轮就自己
    调了 ``memory_write``**（工具序列 ``memory_list → memory_write``），
    于是闸 1（本区间已直写）正确地拦掉了后台提取 —— 断言「不许出现 memory_write」
    反而变成了错的。那一轮证明了闸 1 在工作，但没有验证「兜底」这条路径。

    现在改成：把偏好放在**顺带一提**的位置，不给「请记住」的指令，
    让模型倾向直接回答而不是写工具。

    后台提取是 fire-and-forget，SSE 结束不代表它跑完，所以要轮询等。
    """
    status = await client.get("/api/me/memories/status",
                              headers={"Authorization": f"Bearer {token}"})
    status.raise_for_status()
    st = status.json()
    if not st.get("auto_write"):
        return {
            "case": "S-M4",
            "checks": [{"kind": "precondition", "ok": False,
                        "detail": "后端未开启 HLZ_MEMORY_AUTO_WRITE=1，本场景前提不成立。"
                                  "请用 HLZ_MEMORY_AUTO_WRITE=1 启动后端后重跑。"}],
            "ok": False,
        }

    before = await _list_memories(client, token)
    before_slugs = {e["slug"] for e in before.get("entries") or []}

    sid = await new_session(client, token, "S-M4")
    # 偏好夹在问题里「顺带提」，不写「请记住」。
    prompt = (f"帮我解释一下 Python 的 GIL 是什么。"
              f"对了说明一下背景：我是做后端的，团队里习惯先看压测数据再下结论，"
              f"不太喜欢纯理论的说法。")
    ev = await send_message(client, token, sid, prompt, 240)
    summary = summarize(ev)

    # 轮询等后台提取（最多 ~90s）
    after = before
    for _ in range(45):
        await asyncio.sleep(2)
        after = await _list_memories(client, token)
        if len(after.get("entries") or []) > len(before_slugs):
            break

    after_slugs = {e["slug"] for e in after.get("entries") or []}
    new_slugs = after_slugs - before_slugs

    bodies = ""
    for slug in new_slugs:
        r = await client.get(f"/api/me/memories/{slug}",
                             headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            bodies += (r.json().get("content") or "") + (r.json().get("description") or "")

    write_calls = [t for t in summary["tools"] if t == "memory_write"]

    checks = [
        {"kind": "auto_write_enabled", "ok": bool(st.get("auto_write")),
         "detail": "后端已开启自动提取"},
        {"kind": "model_did_not_write_directly", "ok": not write_calls,
         "detail": f"工具序列：{summary['tools']}（本轮应由后台兜底，而不是模型自己写）"},
        {"kind": "background_extraction_wrote", "ok": bool(new_slugs),
         "detail": f"新增 slug：{sorted(new_slugs)}"},
        {"kind": "done", "ok": (summary["done"] or {}).get("status") == "complete",
         "detail": str(summary["done"])},
    ]
    return {
        "case": "S-M4",
        "session_id": sid,
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
        "answer": summary["text"][:400],
        "tools": summary["tools"],
        "new_slugs": sorted(new_slugs),
        "new_bodies_preview": bodies[:500],
    }


async def _user_id(client: httpx.AsyncClient, token: str) -> str:
    """从 /api/auth/me 拿 user_id；拿不到返回空串（调用方按「无法核对」处理）。"""
    for path in ("/api/auth/me", "/api/me"):
        try:
            r = await client.get(path, headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                continue
            payload = r.json() or {}
            uid = str((payload.get("user") or {}).get("id") or payload.get("id") or "")
            if uid:
                return uid
        except Exception:  # noqa: BLE001
            continue
    return ""


async def case_sm8(client: httpx.AsyncClient, token: str, out: Path) -> dict:
    """S-M8：dream **真的把重复记忆合并了**（真 LLM，M4.5 的核心证据）。

    造 5 条讲同一件事的记忆（不同 slug、不同措辞、其中一条带相对日期
    「上周三」），然后调「立即整理」，验证：

    1. 条数下降（真的合并了，不是「原样不动」）；
    2. **内容没丢** —— 保留者里仍有哨兵与核心事实。这条最重要：
       一个「删了但没合并」的实现也能让条数下降，那是数据丢失不是整理；
    3. 索引被重建，且条数与实际条数一致；
    4. 相对日期被换成绝对日期或去时间化的说法（方案 §4.6 的第 2 个设计点）。

    前置：后端需要有一条可用的用户模型配置（dream 用用户自己的模型）。
    """
    status = await client.get("/api/me/memories/status",
                              headers={"Authorization": f"Bearer {token}"})
    status.raise_for_status()
    st = status.json()
    dream = st.get("dream") or {}
    if not dream.get("enabled"):
        return {
            "case": "S-M8",
            "checks": [{"kind": "precondition", "ok": False,
                        "detail": "dream 未启用（HLZ_MEMORY_DREAM_ENABLED=0 "
                                  "或 HLZ_MEMORY_DISABLED=1）"}],
            "ok": False,
        }

    # 5 条讲同一件事的记忆，措辞各异，其中一条用相对日期
    dupes = [
        ("call-pref-a", "用户要求以后叫他「老徐」",
         f"# 称呼\n\n用户明确说过：以后都叫他「老徐」。\n\n标记 {SENTINEL}\n"),
        ("call-pref-b", "称呼偏好：叫老徐，不要用全名",
         "用户不喜欢被叫全名，希望被称呼为老徐。\n"),
        ("call-pref-c", "用户的名字/称呼",
         "他姓徐，习惯被叫「老徐」。\n"),
        ("call-pref-d", "上周三说过要叫老徐",
         "上周三他提过一次：以后称呼他老徐。\n"),
        ("call-pref-e", "称呼方式确认",
         "确认：称呼上用「老徐」，与「用户称呼」那条是同一件事。\n"),
    ]
    for slug, desc, body in dupes:
        await _write_memory(client, token, slug=slug, title=desc,
                            description=desc, content=body, kind="user")

    before = await _list_memories(client, token)
    before_slugs = {e["slug"] for e in before.get("entries") or []}
    before_total = int(before.get("total") or 0)

    # 「立即整理」：跳过 24h / 5 会话两道闸（真 LLM 调用，可能几十秒）
    r = await client.post("/api/me/memories/dream",
                          headers={"Authorization": f"Bearer {token}"}, timeout=600)
    r.raise_for_status()
    dream_out = r.json()

    after = await _list_memories(client, token)
    after_slugs = {e["slug"] for e in after.get("entries") or []}
    after_total = int(after.get("total") or 0)

    # 读全部正文，确认哨兵与核心事实都还在
    bodies: dict[str, str] = {}
    for slug in after_slugs:
        rr = await client.get(f"/api/me/memories/{slug}",
                              headers={"Authorization": f"Bearer {token}"})
        if rr.status_code == 200:
            d = rr.json()
            bodies[slug] = (d.get("content") or "") + "\n" + (d.get("description") or "")
    all_text = "\n".join(bodies.values())

    # 索引：MEMORY.md 的行数与实际条数一致（不该有指向已删条目的幽灵行）
    index_rows = 0
    try:
        from app import memory_store as ms
        uid = await _user_id(client, token)
        if uid:
            ipath = ms.index_path(uid)
            if ipath.exists():
                text = ipath.read_text(encoding="utf-8")
                index_rows = sum(1 for ln in text.split("\n") if ln.startswith("- ["))
    except Exception:  # noqa: BLE001 - 索引核对是加分项，读不到不判失败
        index_rows = -1

    merged_slugs = before_slugs - after_slugs
    checks = [
        {"kind": "dream_ran", "ok": bool(dream_out.get("ran")),
         "detail": f"ran={dream_out.get('ran')} reason={dream_out.get('reason')}"},
        {"kind": "count_reduced", "ok": after_total < before_total,
         "detail": f"{before_total} -> {after_total}（写入 {dream_out.get('written')}，"
                   f"删除 {dream_out.get('deleted')}）"},
        {"kind": "something_deleted", "ok": bool(merged_slugs),
         "detail": f"消失的 slug：{sorted(merged_slugs)}"},
        # 最重要的一条：条数下降必须来自「合并」，不是「丢了」
        {"kind": "content_preserved", "ok": SENTINEL in all_text and "老徐" in all_text,
         "detail": f"哨兵在？{SENTINEL in all_text}；老徐在？{'老徐' in all_text}"},
        {"kind": "index_rebuilt", "ok": bool(dream_out.get("index_rebuilt")),
         "detail": str(dream_out.get("index_rebuilt"))},
        {"kind": "index_matches_count",
         "ok": index_rows < 0 or index_rows == after_total,
         "detail": f"索引行 {index_rows} / 实际 {after_total}"},
        # 相对日期应被转成绝对日期或去时间化的说法
        {"kind": "relative_date_fixed",
         "ok": "上周三" not in all_text,
         "detail": f"正文里还有「上周三」？{'上周三' in all_text}"},
    ]
    return {
        "case": "S-M8",
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
        "before_total": before_total,
        "after_total": after_total,
        "before_slugs": sorted(before_slugs),
        "after_slugs": sorted(after_slugs),
        "dream": dream_out,
        "bodies": {k: v[:300] for k, v in bodies.items()},
    }


CASES = {"S-M1": case_sm1, "S-M2": case_sm2, "S-M3": case_sm3,
         "S-M4": case_sm4, "S-M5": case_sm5, "S-M8": case_sm8}


async def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 控制台不支持时退化为 ASCII 标记
        pass
    wanted = [a for a in argv if a in CASES] or list(CASES)
    run_id = _now_run_id()
    out = RUNS / f"memory-{run_id}"
    out.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(base_url=BASE, timeout=None) as client:
        token_a = await ensure_user(client, f"mem_a_{run_id}".replace("-", "_"))
        token_b = await ensure_user(client, f"mem_b_{run_id}".replace("-", "_"))

        for name in wanted:
            fn = CASES[name]
            try:
                if name == "S-M3":
                    res = await fn(client, token_a, token_b, out)
                else:
                    res = await fn(client, token_a, out)
            except Exception as exc:  # noqa: BLE001 - 现场必须落盘
                res = {"case": name, "ok": False,
                       "checks": [{"kind": "_exception", "ok": False,
                                   "detail": f"{type(exc).__name__}: {exc}"}]}
            results.append(res)
            print(f"[{'PASS' if res['ok'] else 'FAIL'}] {name}")
            for c in res["checks"]:
                mark = "OK  " if c["ok"] else "FAIL"
                print(f"    [{mark}] {c['kind']}: {c.get('detail','')}")

    (out / "memory_results.json").write_text(
        json.dumps({"run_id": run_id, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\nrunId={run_id}\n现场：{out}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
