"""今天（2026-11-14）全部改动的真机验证 —— 各种场景与边界条件。

只读产品代码，不改任何源码。新场景写在这里，复用 `backend/tests/e2e/harness.py`。

四组：

| 组 | 验什么 | 为什么必须真机 |
|---|---|---|
| A 档位边界 | 三档 × 记忆读写，含 **chat_only 连预取都不注入**（今天刚修，从未 e2e 验过） | 档位闸在装配点，只有真跑一轮才知道哪条通道还开着 |
| B 预取边界 | 选择器开启时注入形态是 `[记忆·相关]`（互斥生效）、不重复注入、超时/失败退化 | 互斥是今天的核心设计，单测只证明函数返回值，不证明真实注入块长什么样 |
| C A2A 长任务 | 生产默认 60s 窗口下的 `WORKING` + 可恢复 | 之前 S-M6 是拿 0.5s 窗口催出来的，要用默认值再验 |
| D A2A 边界 | 幂等、配额、取消、跨用户隔离、未授权 | 协议面，单测覆盖不全 |

跑法（后端须以 `work/_launch_verify.py` 启动）::

    cd backend && python ../tools/a2a_verify/main_cases.py            # 全部
    cd backend && python ../tools/a2a_verify/main_cases.py A B        # 按组
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402

from tests.e2e.harness import (BASE, RUNS, ensure_user, ensure_zhanlu,  # noqa: E402
                               new_session, send_message, summarize)

SENTINEL = "VD7c3e28"
RUN_ID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"
OUT = RUNS / f"verify-{RUN_ID}"


# ---------------------------------------------------------------- 通用小工具

def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _enable_agent(c: httpx.AsyncClient, token: str) -> dict:
    r = await c.post("/api/a2a/agent/enabled", json={"enabled": True}, headers=_h(token))
    r.raise_for_status()
    r = await c.get("/api/a2a/agent", headers=_h(token))
    r.raise_for_status()
    return r.json()


async def _token(c: httpx.AsyncClient, token: str, policy: str, name: str) -> dict:
    r = await c.post("/api/a2a/tokens",
                     json={"name": name, "tool_policy": policy, "expires_in_days": 1},
                     headers=_h(token))
    r.raise_for_status()
    return r.json()


async def _rpc(c: httpx.AsyncClient, path: str, tok: str, method: str,
               params: dict, timeout_s: float = 420) -> dict:
    r = await c.post(path, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params},
                     headers={**_h(tok), "content-type": "application/json"},
                     timeout=timeout_s)
    if r.status_code >= 400:
        return {"_http": r.status_code, "_body": r.text[:400]}
    return r.json()


async def _send(c: httpx.AsyncClient, path: str, tok: str, text: str,
                *, ctx: str | None = None, cfg: dict | None = None,
                mid: str | None = None, timeout_s: float = 420) -> dict:
    msg: dict[str, Any] = {"messageId": mid or f"m_{uuid.uuid4().hex[:12]}",
                           "role": "ROLE_USER", "parts": [{"text": text}]}
    if ctx:
        msg["contextId"] = ctx
    params: dict[str, Any] = {"message": msg}
    if cfg:
        params["configuration"] = cfg
    return await _rpc(c, path, tok, "SendMessage", params, timeout_s=timeout_s)


def _task(resp: dict) -> dict:
    return (resp.get("result") or {}).get("task") or {}


def _state(resp: dict) -> str:
    return (_task(resp).get("status") or {}).get("state", "")


def _text(resp: dict) -> str:
    return "".join(str(p.get("text") or "")
                   for a in (_task(resp).get("artifacts") or [])
                   for p in (a.get("parts") or []) if isinstance(p, dict))


async def _wait_terminal(c: httpx.AsyncClient, path: str, tok: str, task_id: str,
                         limit_s: float = 300) -> tuple[str, str]:
    end = time.time() + limit_s
    while time.time() < end:
        got = await _rpc(c, path, tok, "GetTask", {"id": task_id}, timeout_s=60)
        res = got.get("result") or {}
        st = (res.get("status") or {}).get("state", "")
        if st in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"):
            txt = "".join(str(p.get("text") or "")
                          for a in (res.get("artifacts") or [])
                          for p in (a.get("parts") or []) if isinstance(p, dict))
            return st, txt
        await asyncio.sleep(3)
    return "TIMEOUT", ""


async def _mems(c: httpx.AsyncClient, token: str) -> set[str]:
    r = await c.get("/api/me/memories?limit=200", headers=_h(token))
    r.raise_for_status()
    return {m.get("slug", "") for m in (r.json().get("entries") or [])}


async def _put_mem(c: httpx.AsyncClient, token: str, **body) -> None:
    r = await c.post("/api/me/memories", json=body, headers=_h(token))
    r.raise_for_status()


def _ck(kind: str, name: str, ok: bool, detail: str = "") -> dict:
    return {"kind": kind, "name": name, "ok": bool(ok), "detail": detail}


# ---------------------------------------------------------------- A 档位边界

async def group_a(c: httpx.AsyncClient) -> list[dict]:
    """三档 × 记忆读写。重点是 chat_only 连预取都不能注入（今天刚修）。"""
    out: list[dict] = []

    # --- A1: full 档，模型看得到三件套 ---
    tok = await ensure_user(c, f"va1_{RUN_ID}".replace("-", "_"))
    await ensure_zhanlu(c, tok)
    ag = await _enable_agent(c, tok)
    tk = await _token(c, tok, "full", f"full-{RUN_ID}")
    path = f"/a2a/{ag['agent_id']}"
    r = await _send(c, path, tk["token"],
                    f"不要调用工具，只列出你可用的、以 memory_ 开头的工具名。最后输出 {SENTINEL}")
    t = _text(r)
    out.append(_ck("A1_full_has_write", "memory_write", "memory_write" in t, t[:120]))
    out.append(_ck("A1_sentinel", SENTINEL, SENTINEL in t))

    # --- A2: read_only，有读无写 ---
    tk2 = await _token(c, tok, "read_only", f"ro-{RUN_ID}")
    r = await _send(c, path, tk2["token"],
                    f"不要调用工具，只列出你可用的、以 memory_ 开头的工具名。最后输出 {SENTINEL}")
    t = _text(r)
    out.append(_ck("A2_ro_has_list", "memory_list", "memory_list" in t, t[:120]))
    out.append(_ck("A2_ro_no_write", "memory_write", "memory_write" not in t))

    # --- A3: chat_only，工具面为空 ---
    tk3 = await _token(c, tok, "chat_only", f"co-{RUN_ID}")
    r = await _send(c, path, tk3["token"],
                    f"不要调用工具，只列出你可用的、以 memory_ 开头的工具名；一个都没有就说 NONE。"
                    f"最后输出 {SENTINEL}")
    t = _text(r)
    out.append(_ck("A3_co_no_memory_tool", "no memory_*",
                   "memory_list" not in t and "memory_read" not in t, t[:120]))

    # --- A4: chat_only 真的不注入记忆（今天修的那条）---
    # 先写一条带哨兵的记忆，再用 chat_only 问它
    secret = f"CHATONLY_{uuid.uuid4().hex[:8]}"
    await _put_mem(c, tok, slug="a4-secret", title="A4 哨兵",
                   description=f"内部值 {secret}", content=f"# A4\n\n{secret}\n",
                   kind="project")
    r = await _send(c, path, tk3["token"],
                    "我记得之前告诉过你一个内部值，是什么？没有就说「查不到」，不要编。")
    t = _text(r)
    out.append(_ck("A4_chat_only_no_memory_read", secret, secret not in t, t[:200]))

    # --- A5: read_only 能读到那条记忆（读侧保留）---
    r = await _send(c, path, tk2["token"],
                    "我记得之前告诉过你一个内部值，是什么？没有就说「查不到」，不要编。")
    t = _text(r)
    out.append(_ck("A5_read_only_can_read_memory", secret, secret in t, t[:200]))

    return out


# ---------------------------------------------------------------- B 预取边界

async def group_b(c: httpx.AsyncClient) -> list[dict]:
    """选择器开启时的注入形态、去重、退化。"""
    out: list[dict] = []
    tok = await ensure_user(c, f"vb1_{RUN_ID}".replace("-", "_"))
    await ensure_zhanlu(c, tok)

    # 写几条互不相同的记忆，其中一条与下面的问题语义相关但无字面重合
    await _put_mem(c, tok, slug="b-deploy", title="发布窗口",
                   description="生产发布只能在工作日 10:00-16:00 之间做",
                   content="# 发布\n\n工作日 10:00-16:00\n", kind="project")
    await _put_mem(c, tok, slug="b-unrelated", title="端口约定",
                   description="8000 是后端，5173 是前端 dev",
                   content="# 端口\n\n8000/5173\n", kind="reference")

    sid = await new_session(c, tok, f"B-{RUN_ID}")
    # 问一句与 b-deploy 语义相关、但**没有字面重合**的话
    events = await send_message(c, tok, sid,
                                "线上能不能下午再发版？只要回答能或不能，并说明依据。",
                                timeout_s=300)
    s = summarize(events)
    text = s["text"]
    blocks = [e for e in events if e.get("type") == "message_injected"]

    out.append(_ck("B1_injected_some", f"{len(blocks)} blocks", len(blocks) >= 1,
                   json.dumps([b.get("text", "")[:80] for b in blocks],
                              ensure_ascii=False)))
    # 互斥：选择器开着时应该是 [记忆·相关]，不该是裸 [记忆]
    joined = "".join(b.get("text", "") for b in blocks)
    out.append(_ck("B2_selector_prefix", "[记忆·相关]",
                   "[记忆·相关]" in joined, joined[:200]))
    out.append(_ck("B2b_no_plain_keyword_block", "no bare [记忆]",
                   not any(t.startswith("[记忆]") for t in
                           (b.get("text", "") for b in blocks)), joined[:200]))
    out.append(_ck("B3_disclaimer", "仅为可能相关",
                   "可能" in joined and "相关" in joined, joined[:200]))
    # 同一条记忆不该出现两次
    if "b-deploy" in joined or "发布" in joined:
        out.append(_ck("B4_no_duplicate", "single occurrence",
                       joined.count("[记忆·相关]") <= 1, joined[:200]))
    out.append(_ck("B5_answer_uses_memory", "下午/工作日",
                   ("不能" in text) or ("工作日" in text) or ("16" in text), text[:200]))
    return out


# ---------------------------------------------------------------- C A2A 长任务

async def group_c(c: httpx.AsyncClient) -> list[dict]:
    """生产默认 60s 同步窗口下的长任务语义。"""
    out: list[dict] = []
    tok = await ensure_user(c, f"vc1_{RUN_ID}".replace("-", "_"))
    await ensure_zhanlu(c, tok)
    ag = await _enable_agent(c, tok)
    tk = await _token(c, tok, "full", f"c-{RUN_ID}")
    path = f"/a2a/{ag['agent_id']}"

    # 不让调用方指定窗口 —— 走服务端默认（生产形态）
    r = await _send(c, path, tk["token"],
                    f"用 code_run 跑 python -c \"import time;time.sleep(65);print(7*6)\"，"
                    f"然后复述输出并输出 {SENTINEL}")
    st = _state(r)
    task = _task(r)
    tid = task.get("id", "")
    out.append(_ck("C1_first_state", st, st in ("TASK_STATE_WORKING", "TASK_STATE_COMPLETED"),
                   st))
    if st == "TASK_STATE_WORKING":
        out.append(_ck("C2_still_running", str(task.get("stillRunning")),
                       task.get("stillRunning") is True))
        out.append(_ck("C3_poll", str((task.get("poll") or {}).get("method")),
                       (task.get("poll") or {}).get("method") == "GetTask"))
        out.append(_ck("C4_subscribe", str((task.get("subscribe") or {}).get("method")),
                       (task.get("subscribe") or {}).get("method") == "SubscribeToTask"))
        mtext = ""
        for p in ((task.get("status") or {}).get("message") or {}).get("parts") or []:
            mtext += str(p.get("text") or "")
        out.append(_ck("C5_message_says_running", mtext[:40], "仍在运行" in mtext))
        fst, ftxt = await _wait_terminal(c, path, tk["token"], tid)
        out.append(_ck("C6_recover_terminal", fst, fst == "TASK_STATE_COMPLETED"))
        out.append(_ck("C7_recovered_sentinel", SENTINEL, SENTINEL in ftxt, ftxt[:200]))
    else:
        out.append(_ck("C1b_completed_within_60s", st, st == "TASK_STATE_COMPLETED",
                       "任务比预期快，未触发超时分支（不算失败，但没测到超时路径）"))
        out.append(_ck("C2_artifacts", SENTINEL, SENTINEL in _text(r), _text(r)[:200]))
    return out


# ---------------------------------------------------------------- D A2A 边界

async def group_d(c: httpx.AsyncClient) -> list[dict]:
    """幂等、配额、取消、跨用户、未授权。"""
    out: list[dict] = []
    tok = await ensure_user(c, f"vd1_{RUN_ID}".replace("-", "_"))
    await ensure_zhanlu(c, tok)
    ag = await _enable_agent(c, tok)
    tk = await _token(c, tok, "full", f"d-{RUN_ID}")
    path = f"/a2a/{ag['agent_id']}"

    # D1 幂等：同一 messageId 两次 -> 同一 task id
    mid = f"idem_{uuid.uuid4().hex[:10]}"
    r1 = await _send(c, path, tk["token"], "回复 OK", mid=mid, timeout_s=180)
    r2 = await _send(c, path, tk["token"], "回复 OK", mid=mid, timeout_s=180)
    out.append(_ck("D1_idempotent", f"{_task(r1).get('id')}=={_task(r2).get('id')}",
                   _task(r1).get("id") and _task(r1).get("id") == _task(r2).get("id")))

    # D2 未授权
    r = await c.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "GetTask",
                                 "params": {"id": "x"}})
    out.append(_ck("D2_unauthorized", str(r.status_code), r.status_code == 401))

    # D3 拿 A 的 token 打 B 的 agent
    tok2 = await ensure_user(c, f"vd2_{RUN_ID}".replace("-", "_"))
    ag2 = await _enable_agent(c, tok2)
    r = await c.post(f"/a2a/{ag2['agent_id']}",
                     json={"jsonrpc": "2.0", "id": 1, "method": "GetTask",
                           "params": {"id": "x"}}, headers=_h(tk["token"]))
    out.append(_ck("D3_agent_mismatch", str(r.status_code), r.status_code == 403))

    # D4 未知方法
    got = await _rpc(c, path, tk["token"], "NoSuchMethod", {})
    out.append(_ck("D4_method_not_found", str((got.get("error") or {}).get("code")),
                   (got.get("error") or {}).get("code") == -32601))

    # D5 取消
    r = await _send(c, path, tk["token"],
                    "用 code_run 跑 python -c \"import time;time.sleep(120)\"",
                    cfg={"returnImmediately": True})
    tid = _task(r).get("id", "")
    if tid:
        got = await _rpc(c, path, tk["token"], "CancelTask", {"id": tid})
        cst = ((got.get("result") or {}).get("status") or {}).get("state", "")
        out.append(_ck("D5_cancel", cst, cst == "TASK_STATE_CANCELED", str(got)[:200]))
    else:
        out.append(_ck("D5_cancel", "no task id", False))

    # D6 GetTask 不存在的 id
    got = await _rpc(c, path, tk["token"], "GetTask", {"id": "task_does_not_exist"})
    out.append(_ck("D6_task_not_found", str((got.get("error") or {}).get("code")),
                   (got.get("error") or {}).get("code") == -32001))

    # D7 跨用户记忆隔离（负对照）
    sa = f"ISO_{uuid.uuid4().hex[:8]}"
    await _put_mem(c, tok, slug="d7-secret", title="D7", description=f"值是 {sa}",
                   content=f"# D7\n\n{sa}\n", kind="project")
    tk2b = await _token(c, tok2, "full", f"d7-{RUN_ID}")
    ra = await _send(c, path, tk["token"], "我之前告诉过你的内部值是什么？没有就说查不到。")
    rb = await _send(c, f"/a2a/{ag2['agent_id']}", tk2b["token"],
                     "我之前告诉过你的内部值是什么？没有就说查不到。")
    out.append(_ck("D7a_owner_sees", sa, sa in _text(ra), _text(ra)[:160]))
    out.append(_ck("D7b_other_user_cannot", sa, sa not in _text(rb), _text(rb)[:160]))
    return out


GROUPS = {"A": group_a, "B": group_b, "C": group_c, "D": group_d}


async def main(argv: list[str]) -> int:
    wanted = [a.upper() for a in argv if a.upper() in GROUPS] or list(GROUPS)
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"runId={RUN_ID}\n输出={OUT}\n后端={BASE}\n组={wanted}\n")

    all_checks: list[dict] = []
    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        for g in wanted:
            print("=" * 66)
            print(f"组 {g}")
            print("=" * 66)
            started = time.time()
            try:
                checks = await GROUPS[g](c)
            except Exception as exc:  # noqa: BLE001
                checks = [_ck(f"{g}_exception", type(exc).__name__, False, str(exc)[:300])]
            for ck in checks:
                print(f"  [{'OK  ' if ck['ok'] else 'FAIL'}] {ck['kind']}: {ck['name']}"
                      + (f"  | {ck['detail'][:90]}" if ck.get("detail") else ""))
            print(f"  --> {sum(1 for x in checks if x['ok'])}/{len(checks)} "
                  f"({round(time.time() - started, 1)}s)\n")
            all_checks.extend({"group": g, **x} for x in checks)

    (OUT / "checks.json").write_text(
        json.dumps(all_checks, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")
    bad = [x for x in all_checks if not x["ok"]]
    print("=" * 66)
    print(f"总计 {len(all_checks) - len(bad)}/{len(all_checks)} 通过"
          f"，{len(bad)} 失败")
    for x in bad:
        print(f"  ✗ {x['group']} {x['kind']}: {x['name']}  {x.get('detail', '')[:120]}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
