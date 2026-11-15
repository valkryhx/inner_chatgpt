"""针对性真机自测（第二轮）：真实协作任务 × 断线续看。

## 为什么要有这一批

第一轮的 `live_selftest.py` 验的是**帧层**（心跳 / seq / 重放缓冲 / sync 降级），
prompt 用的是纯文本长输出和 `code_run` 睡眠 —— **那一轮里根本没有子代理**。
而本轮改动恰恰对协作场景影响最大：

| 场景 | 恢复路径 | 这条路径上最容易坏的地方 |
|---|---|---|
| 子代理卡片 | `blocks` 里的 `type: "subagent"` 块 | `sync` 若取「已落库快照」而不是「此刻 blocks 引用」，卡片会缺一段（阶段 2 改动的真正受益者就是这里） |
| Team 任务板 | 走 `team_event`，**不落 `blocks`**；刷新靠 `GET /api/sessions/{sid}/team` 从 DB 恢复 | 这是**另一条独立恢复路径**，与 blocks 快照互不覆盖 |
| `/stop` 后立刻续看 | job 已 `finish`、消息已终态 | 会撞「没有活 job」分支，必须回 `done` 而不是 404，也不能凭空新建消息 |

## 判据

每条都落到具体证据：SSE 原始帧 / `GET /api/sessions/{sid}` 的持久化 blocks /
`GET /api/sessions/{sid}/team` 的任务板 / 库里的消息条数。
不接受「机制看起来触发了」。

## 跑法

    python tools/turn_job_verify/resume_collab.py --base http://127.0.0.1:8000

前置：后端在跑；用生产默认心跳（15s）即可，本批不依赖压小的上限。
"""
from __future__ import annotations

import json
import secrets
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "turn_job_verify"))
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from live_selftest import (  # noqa: E402  —— 复用同一套 SSE 读取与解析
    Client,
    get_messages,
    parse_sse_frames,
    stream_get,
    stream_post,
)

RUN_ID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}-rc"
OUT = ROOT / "backend" / "tests" / "e2e" / "runs" / RUN_ID
results: list[tuple[str, bool, str]] = []

# 每个场景**各自一份** events.jsonl。
#
# 第一版把三个会话的 SSE 合并进一个文件，结果 `python -m tests.e2e.audit` 给出
# 一堆假缺陷：`team_id` 取自 A（A 根本没建 team），任务态取自 B，于是
# `task_states != db_task_states`；`zombie_running` 也因为「故意中途断开、
# subagent_end 不在截取窗口内」而必然非空。
# **审计工具是单场景设计的**，喂它跨会话的混合流只会得到串味的结论。
EVENTS: dict[str, list[dict]] = {}
_cur = ""


def _begin(case: str) -> None:
    global _cur
    _cur = case
    EVENTS.setdefault(case, [])


def _emit(evs: list[dict]) -> None:
    EVENTS.setdefault(_cur, []).extend(evs)


SENTINEL = "SENTINEL=" + secrets.token_hex(3)



def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  | {detail}" if detail else ""), flush=True)


def frames_to_events(frames: list[dict]) -> list[dict]:
    """把原始 SSE 帧还原成事件对象（与 harness 同形状），供审计工具消费。"""
    out = []
    for f in frames:
        if f.get("comment"):
            continue
        data = (f.get("data") or "").strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            out.append({"type": "_unparsed", "raw": data[:200]})
            continue
        if isinstance(obj, dict):
            if not obj.get("type"):
                obj["type"] = f.get("event") or ""
            out.append(obj)
    return out


def sync_payload(frames: list[dict]) -> dict | None:
    for f in frames:
        if f.get("event") == "sync":
            try:
                return json.loads(f.get("data") or "{}")
            except json.JSONDecodeError:
                return None
    return None


def apply_resume_frames(seed_blocks: list[dict], events: list[dict]) -> list[dict]:
    """按**前端真实算法**把续看帧叠加到 blocks 上，返回重建后的 blocks。

    为什么要模拟而不是直接读 `sync`：`sync` 只是**降级分支**。真实契约是
    （`frontend/src/App.tsx: resumeIfRunning`）：

        种子 = `loadMessages()` 拿到的 blocks（库里那份，含子代理卡片）
        然后逐帧叠加：sync 替换全部 / text_delta 追加 / tool_start 新增 /
                      tool_result 回填 / done 收口

    `after=0` 在生产默认 2000 帧缓冲下**几乎总是走增量重放**而不是 sync ——
    第一版断言「首帧必须是 sync」是我把降级分支当成了主路径，属于判据错，
    不是产品缺陷。这里改成验「两条分支叠加后都能得到正确状态」。
    """
    blocks = [dict(b) for b in seed_blocks]
    for ev in events:
        t = ev.get("type")
        if t == "sync":
            blocks = [dict(b) for b in (ev.get("blocks") or [])]
        elif t in ("text_delta", "thinking_delta"):
            kind = "text" if t == "text_delta" else "reasoning"
            last = blocks[-1] if blocks else None
            if last and last.get("type") == kind:
                blocks = blocks[:-1] + [{**last, "text": (last.get("text") or "") + (ev.get("text") or "")}]
            else:
                blocks = blocks + [{"type": kind, "text": ev.get("text") or ""}]
        elif t == "tool_start":
            blocks = blocks + [{"type": "tool", "id": ev.get("id"), "name": ev.get("name"),
                                "input": ev.get("input")}]
        elif t == "tool_result":
            blocks = [{**b, "result": ev.get("content")}
                      if b.get("type") == "tool" and b.get("id") == ev.get("id") else b
                      for b in blocks]
        elif t == "subagent_start":
            blocks = blocks + [{"type": "subagent", "agent_id": ev.get("agent_id"),
                                "name": ev.get("name"), "role": ev.get("role"),
                                "status": "running", "task": ev.get("task"), "events": [],
                                "result": "", "turns": 0}]
        elif t == "subagent_end":
            blocks = [{**b, "status": ev.get("status", "completed"),
                       "result": ev.get("result", ""), "turns": ev.get("turns", 0)}
                      if b.get("type") == "subagent" and b.get("agent_id") == ev.get("agent_id")
                      else b for b in blocks]
        elif t == "subagent_status":
            blocks = [{**b, "status": ev.get("status")}
                      if b.get("type") == "subagent" and b.get("agent_id") == ev.get("agent_id")
                      else b for b in blocks]
    return blocks


def last_assistant(client: Client, sid: str) -> dict:
    msgs = get_messages(client, sid)
    return next((m for m in reversed(msgs) if m.get("role") == "assistant"), {}) or {}


def wait_terminal(client: Client, sid: str, timeout_s: float) -> dict:
    """轮询库里的 assistant 终态。真实模型耗时不可控，只能等。"""
    t0 = time.monotonic()
    last: dict = {}
    while time.monotonic() - t0 < timeout_s:
        last = last_assistant(client, sid)
        if last.get("status") in ("complete", "stopped", "error"):
            return last
        time.sleep(3)
    return last


def sub_blocks(blocks: list[dict]) -> list[dict]:
    return [b for b in blocks if b.get("type") == "subagent"]


# ------------------------------------------------------------------ 场景 A

SUB_PROMPT = (
    "派一个子代理，让它调用 code_run 执行下面这段 Python，并把输出原样复述给我：\n"
    "```python\n"
    "import time\n"
    "print('start', flush=True)\n"
    "time.sleep(50)\n"
    f"print('SUB DONE {SENTINEL}', flush=True)\n"
    "```\n"
    "派完之后用 wait_agent 等它（中途超时是正常的，不要重新 spawn，继续等），\n"
    "拿到结果后把子代理的原始输出告诉我。"
)


def case_a_subagent_resume(client: Client) -> str:
    """子代理任务跑到一半断线，续看后卡片必须还能重建出来。"""
    sid = client.new_session("A-subagent-resume")
    gen = f"g-a-{RUN_ID}"
    print(f"\n--- A 子代理任务中途断线续看  sid={sid}", flush=True)

    raw1 = stream_post(client, sid, SUB_PROMPT, gen, stop_after_s=14.0)
    _emit(frames_to_events(parse_sse_frames(raw1)))
    msg_at_cut = last_assistant(client, sid)
    status_at_cut = msg_at_cut.get("status", "none")
    count_at_cut = len(get_messages(client, sid))
    seed_blocks = list(msg_at_cut.get("blocks") or [])

    # 续看：after=0（与前端一致 —— 由后端决定增量还是 sync）
    raw2, _ = stream_get(client, sid, after=0, window_s=20.0)
    frames2 = parse_sse_frames(raw2)
    ev2 = frames_to_events(frames2)
    _emit(ev2)
    sync = sync_payload(frames2)
    rebuilt = apply_resume_frames(seed_blocks, ev2)
    rebuilt_subs = sub_blocks(rebuilt)

    record("A-1 断开时这一轮仍在 streaming（否则没测到续看）",
           status_at_cut == "streaming", f"断开时 status={status_at_cut}")
    record("A-2 续看有内容（增量重放或 sync 快照，二者其一）",
           bool(ev2) and (sync is not None or any(e.get("type") for e in ev2)),
           f"帧类型={[f.get('event') or 'comment' for f in frames2][:4]} "
           f"走的是={'sync 快照' if sync else '增量重放'}")
    record("A-3 按前端算法重建后拿到子代理卡片（真实恢复契约）",
           len(rebuilt_subs) >= 1,
           f"种子 blocks={len(seed_blocks)} → 重建后={len(rebuilt)} "
           f"其中 subagent={len(rebuilt_subs)}"
           + (f" 首个={rebuilt_subs[0].get('agent_id')}/{rebuilt_subs[0].get('status')}"
              if rebuilt_subs else ""))
    record("A-4 续看不新建消息", len(get_messages(client, sid)) == count_at_cut,
           f"{count_at_cut} → {len(get_messages(client, sid))}")


    final = wait_terminal(client, sid, 240)
    fb = sub_blocks(final.get("blocks") or [])
    text = final.get("content") or ""
    ended = [b for b in fb if b.get("status") not in ("running", "starting")]
    record("A-5 最终落库终态 complete", final.get("status") == "complete",
           f"status={final.get('status')}")
    record("A-6 子代理卡片终态非 running", bool(fb) and len(ended) == len(fb),
           f"卡片={[(b.get('name'), b.get('status')) for b in fb]}")
    record("A-7 子代理结果与主答复里都有哨兵", SENTINEL in json.dumps(fb, ensure_ascii=False)
           and SENTINEL in text,
           f"卡片含哨兵={SENTINEL in json.dumps(fb, ensure_ascii=False)} 正文含哨兵={SENTINEL in text}")

    # 收尾：此时已无活 job，续看应回 done（不是 404）
    raw3, _ = stream_get(client, sid, after=999999, window_s=8.0)
    f3 = parse_sse_frames(raw3)
    done = next((x for x in f3 if x.get("event") == "done"), None)
    record("A-8 无活 job 时续看回 done", done is not None,
           f"帧={[x.get('event') or 'comment' for x in f3]}")
    return sid


# ------------------------------------------------------------------ 场景 B

TEAM_PROMPT = (
    "建一个团队任务板，创建 2 个任务：\n"
    "任务 1：用 code_run 跑一段 Python，sleep 30 秒后打印 T1 DONE；\n"
    "任务 2：用 code_run 跑一段 Python，sleep 30 秒后打印 T2 DONE。\n"
    "派 1 个 teammate 依次完成这两个任务（它也可以再派别人，随便）。\n"
    f"每个任务的产出里都要原样包含这个字符串：{SENTINEL}。\n"
    "全部结束后把任务板上每个任务 id 与最终状态汇总给我。"
)


def case_b_team_resume(client: Client) -> str:
    """Team 任务板跑到一半断线：卡片靠 blocks、任务板靠 team API，两条路都要对。"""
    sid = client.new_session("B-team-resume")
    gen = f"g-b-{RUN_ID}"
    print(f"\n--- B Team 任务中途断线续看  sid={sid}", flush=True)

    raw1 = stream_post(client, sid, TEAM_PROMPT, gen, stop_after_s=16.0)
    _emit(frames_to_events(parse_sse_frames(raw1)))
    msg_at_cut = last_assistant(client, sid)
    status_at_cut = msg_at_cut.get("status", "none")
    count_at_cut = len(get_messages(client, sid))
    seed_blocks = list(msg_at_cut.get("blocks") or [])
    team_at_cut = client.get(f"/api/sessions/{sid}/team")
    tasks_cut = team_at_cut.get("tasks") or []

    raw2, _ = stream_get(client, sid, after=0, window_s=20.0)
    frames2 = parse_sse_frames(raw2)
    ev2 = frames_to_events(frames2)
    _emit(ev2)
    rebuilt = apply_resume_frames(seed_blocks, ev2)

    record("B-1 断开时这一轮仍在 streaming", status_at_cut == "streaming",
           f"status={status_at_cut}")
    record("B-2 断开瞬间任务板已可从 DB 读出（独立于 SSE 的恢复路径）",
           len(tasks_cut) >= 1,
           f"tasks={[(t.get('task_id'), t.get('status')) for t in tasks_cut]}")
    record("B-3 续看叠加后成员卡片还在（真实恢复契约）",
           len(sub_blocks(rebuilt)) >= 1,
           f"种子={len(seed_blocks)} → 重建={len(rebuilt)} "
           f"subagent={len(sub_blocks(rebuilt))}，走的是="
           f"{'sync' if sync_payload(frames2) else '增量'}")
    record("B-3b 续看不新建消息", len(get_messages(client, sid)) == count_at_cut,
           f"{count_at_cut} → {len(get_messages(client, sid))}")


    final = wait_terminal(client, sid, 600)
    team_final = client.get(f"/api/sessions/{sid}/team")
    tasks_final = team_final.get("tasks") or []
    states = {t.get("task_id"): t.get("status") for t in tasks_final}
    text = final.get("content") or ""
    record("B-4 最终落库终态 complete", final.get("status") == "complete",
           f"status={final.get('status')}")
    record("B-5 断线未导致任务滞留 running", bool(tasks_final)
           and all(v == "completed" for v in states.values()),
           f"任务终态={states}")
    record("B-6 任务数不少于建的两个", len(tasks_final) >= 2, f"共 {len(tasks_final)} 个")
    fb = sub_blocks(final.get("blocks") or [])
    record("B-7 成员卡片全部收口（无 running）", bool(fb)
           and all(b.get("status") not in ("running", "starting") for b in fb),
           f"卡片={[(b.get('name'), b.get('status')) for b in fb]}")
    record("B-8 主答复里复述了哨兵", SENTINEL in text, f"正文长度={len(text)}")
    return sid


# ------------------------------------------------------------------ 场景 C

def case_c_stop_then_resume(client: Client) -> str:
    """主动停止后立刻续看：必须回 done，且不产生新消息。"""
    sid = client.new_session("C-stop-resume")
    gen = f"g-c-{RUN_ID}"
    print(f"\n--- C 停止后立刻续看  sid={sid}", flush=True)

    raw1 = stream_post(client, sid, SUB_PROMPT, gen, stop_after_s=10.0)
    _emit(frames_to_events(parse_sse_frames(raw1)))
    client.post(f"/api/sessions/{sid}/stop", {"generation_id": gen})
    time.sleep(2.0)
    count_after_stop = len(get_messages(client, sid))

    raw2, _ = stream_get(client, sid, after=0, window_s=10.0)
    f2 = parse_sse_frames(raw2)
    _emit(frames_to_events(f2))
    done = next((x for x in f2 if x.get("event") == "done"), None)
    payload = {}
    if done is not None:
        try:
            payload = json.loads(done.get("data") or "{}")
        except json.JSONDecodeError:
            payload = {}
    status = last_assistant(client, sid).get("status", "none")

    record("C-1 停止后 assistant 不留在 streaming", status in ("stopped", "complete"),
           f"status={status}")
    record("C-2 停止后续看回 done（不是 404/空连接）", done is not None,
           f"帧={[x.get('event') or 'comment' for x in f2]} payload_status={payload.get('status')}")
    record("C-3 续看不新建消息", len(get_messages(client, sid)) == count_after_stop,
           f"{count_after_stop} → {len(get_messages(client, sid))}")
    record("C-4 停止不把已产出内容丢掉", bool((last_assistant(client, sid).get("blocks") or [])),
           f"blocks={(last_assistant(client, sid).get('blocks') or []) and len(last_assistant(client, sid)['blocks'])}")
    return sid


# ------------------------------------------------------------------ main

def _write_case_dir(case: str, sid: str, checks: list[dict]) -> Path:
    """每个场景写成**独立 run 目录**，这样 `tests.e2e.audit` 才能逐场景给结论。

    审计工具从 `events.jsonl` 里取首个 `team_id`、再拿它去 DB 比任务态；
    把三个会话混进一个文件，A 的会话没有 team 就会让 B 的任务态全部对不上。
    """
    d = OUT / case
    d.mkdir(parents=True, exist_ok=True)
    evs = EVENTS.get(case) or []
    (d / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    (d / "summary.json").write_text(
        json.dumps({"run_id": f"{RUN_ID}-{case}", "case": case, "session_id": sid,
                    "checks": checks, "ok": all(c["ok"] for c in checks)},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def main(argv: list[str]) -> int:
    base = "http://127.0.0.1:8000"
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]
    # 只跑某几个场景（复现特定缺陷时省时间）：--only B / --only BC
    only = ""
    if "--only" in argv:
        only = argv[argv.index("--only") + 1].upper()
    OUT.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print(f"协作任务 × 断线续看  runId={RUN_ID}  base={base}")
    print(f"哨兵={SENTINEL}")
    print("=" * 72, flush=True)

    client = Client(base)
    sids: dict[str, str] = {}
    case_checks: dict[str, list[dict]] = {}
    try:
        client.setup_user()
        print(f"  用户就绪\n", flush=True)
        for key, fn in (("A", case_a_subagent_resume),
                        ("B", case_b_team_resume),
                        ("C", case_c_stop_then_resume)):
            if only and key not in only:
                continue
            _begin(key)
            before = len(results)
            sids[key] = fn(client)
            case_checks[key] = [{"name": n, "ok": ok, "detail": d}
                                for n, ok, d in results[before:]]
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        results.append((f"异常：{type(exc).__name__}", False, str(exc)[:160]))

    dirs = {k: str(_write_case_dir(k, sids.get(k, ""), v)) for k, v in case_checks.items()}
    failed = [n for n, ok, _ in results if not ok]
    summary = {
        "run_id": RUN_ID,
        "base": base,
        "sentinel": SENTINEL,
        "sessions": sids,
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results],
        "case_dirs": dirs,
        "ok": not failed,
        "dir": str(OUT),
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 72)
    print(f"通过 {len(results) - len(failed)} / 失败 {len(failed)}")
    for n in failed:
        print(f"  ✗ {n}")
    print(f"现场：{OUT}")
    for k in dirs:
        print(f"审计 {k}：cd backend && python -m tests.e2e.audit {OUT.name}/{k}")
    print("=" * 72)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
