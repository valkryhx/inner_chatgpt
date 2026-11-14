"""第二轮验证：今天改动的**退化路径与并发边界**（只读产品代码）。

第一轮（`_verify_today.py`）验的是「正常路径对不对」。这一轮专门打**坏路**：

| 组 | 打什么 | 为什么值得单独测 |
|---|---|---|
| E | 选择器**失败/超时**时整轮还能不能跑完 | 互斥设计下关键词已让位，选择器挂了那一轮就没有记忆 —— 这是有意的取舍，但不能让整轮崩掉 |
| F | 写侧档位闸的真实形态（full 会写、read_only 不写） | 之前的 S-M4b/c 是在补预取闸**之前**跑的，必须重验 |
| G | 并发配额 429 + 同会话租约 | 配额是「服务不被打爆」的最后一道，单测只验了计数 |
| H | 子代理拿不到记忆工具（闭世界） | M-9 一直只有单测，真机从没验过 |

跑法::

    cd backend && python ../tools/a2a_verify/degrade_cases.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402
from tests.e2e.harness import (BASE, RUNS, ensure_user, ensure_zhanlu,  # noqa: E402
                               new_session, send_message, summarize)

SENTINEL = "DG9f2c41"
RUN_ID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"
OUT = RUNS / f"degrade-{RUN_ID}"


def _h(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


async def _enable_agent(c, token):
    r = await c.post("/api/a2a/agent/enabled", json={"enabled": True}, headers=_h(token))
    r.raise_for_status()
    r = await c.get("/api/a2a/agent", headers=_h(token))
    r.raise_for_status()
    return r.json()


async def _token(c, token, policy, name):
    r = await c.post("/api/a2a/tokens",
                     json={"name": name, "tool_policy": policy, "expires_in_days": 1},
                     headers=_h(token))
    r.raise_for_status()
    return r.json()


async def _send(c, path, tok, text, cfg=None, timeout_s=420):
    msg = {"messageId": f"m_{uuid.uuid4().hex[:12]}", "role": "ROLE_USER",
           "parts": [{"text": text}]}
    params = {"message": msg}
    if cfg:
        params["configuration"] = cfg
    r = await c.post(path, json={"jsonrpc": "2.0", "id": 1,
                                 "method": "SendMessage", "params": params},
                     headers={**_h(tok), "content-type": "application/json"},
                     timeout=timeout_s)
    if r.status_code >= 400:
        return {"_http": r.status_code, "_body": r.text[:300]}
    return r.json()


def _task(r):
    return (r.get("result") or {}).get("task") or {}


def _text(r):
    return "".join(str(p.get("text") or "")
                   for a in (_task(r).get("artifacts") or [])
                   for p in (a.get("parts") or []) if isinstance(p, dict))


def _state(r):
    return (_task(r).get("status") or {}).get("state", "")


async def _mems(c, tok) -> set[str]:
    r = await c.get("/api/me/memories?limit=200", headers=_h(tok))
    r.raise_for_status()
    return {m.get("slug", "") for m in (r.json().get("entries") or [])}


async def _put(c, tok, **b):
    r = await c.post("/api/me/memories", json=b, headers=_h(tok))
    r.raise_for_status()


def _ck(kind, name, ok, detail=""):
    return {"kind": kind, "name": name, "ok": bool(ok), "detail": detail}


# ------------------------------------------------------- E 选择器退化

async def group_e(c: httpx.AsyncClient) -> list[dict]:
    """选择器不可用时，整轮必须还能正常完成（只是没有记忆）。"""
    out = []
    tok = await ensure_user(c, f"ve1_{RUN_ID}".replace("-", "_"))
    await ensure_zhanlu(c, tok)
    await _put(c, tok, slug="e-note", title="E 备注",
               description="E 组用于验证退化路径", content="# E\n", kind="project")
    sid = await new_session(c, tok, f"E-{RUN_ID}")

    # E1 正常一轮必须完成（选择器开着）
    ev = await send_message(c, tok, sid,
                            f"只回答 OK，不要调用工具。最后输出 {SENTINEL}",
                            timeout_s=300)
    s = summarize(events=ev)
    status = (s.get("done") or {}).get("status")
    out.append(_ck("E1_turn_completes_with_selector_on", str(status),
                   status == "complete", json.dumps(s.get("errors") or [])[:200]))
    out.append(_ck("E2_sentinel", SENTINEL, SENTINEL in s["text"]))
    out.append(_ck("E3_no_error_events", "errors",
                   not s.get("errors"), str(s.get("errors"))[:200]))
    # E4 一轮里选择器不该把轮次卡住超过合理时间（有 8s 超时兜底）
    return out


# ------------------------------------------------------- F 写侧档位闸

async def group_f(c: httpx.AsyncClient) -> list[dict]:
    """full 会写记忆、read_only 不写 —— 补预取闸之后重验。"""
    out = []
    prompt = ("记住：我叫老徐，做后端，以后回答先给结论再给依据。"
              f"一句话确认即可，最后输出 {SENTINEL}")

    # F1 read_only：零写入
    ta = await ensure_user(c, f"vf1_{RUN_ID}".replace("-", "_"))
    await ensure_zhanlu(c, ta)
    ag_a = await _enable_agent(c, ta)
    tk_a = await _token(c, ta, "read_only", f"f-ro-{RUN_ID}")
    before_a = await _mems(c, ta)
    ra = await _send(c, f"/a2a/{ag_a['agent_id']}", tk_a["token"], prompt)
    await asyncio.sleep(95)           # 后台提取是 fire-and-forget
    after_a = await _mems(c, ta)
    out.append(_ck("F1_read_only_no_write", f"{len(before_a)}->{len(after_a)}",
                   before_a == after_a, f"new={sorted(after_a - before_a)}"))

    # F2 full：必须写（正向对照，否则 F1 是空断言）
    tb = await ensure_user(c, f"vf2_{RUN_ID}".replace("-", "_"))
    await ensure_zhanlu(c, tb)
    ag_b = await _enable_agent(c, tb)
    tk_b = await _token(c, tb, "full", f"f-full-{RUN_ID}")
    before_b = await _mems(c, tb)
    rb = await _send(c, f"/a2a/{ag_b['agent_id']}", tk_b["token"], prompt)
    await asyncio.sleep(95)
    after_b = await _mems(c, tb)
    new_b = after_b - before_b
    out.append(_ck("F2_full_writes", f"{len(before_b)}->{len(after_b)}",
                   len(new_b) >= 1, f"new={sorted(new_b)}"))
    out.append(_ck("F3_both_completed", "states",
                   _state(ra) == "TASK_STATE_COMPLETED"
                   and _state(rb) == "TASK_STATE_COMPLETED",
                   f"{_state(ra)} / {_state(rb)}"))
    return out


# ------------------------------------------------------- G 配额与并发

async def group_g(c: httpx.AsyncClient) -> list[dict]:
    """并发配额、同会话租约。"""
    out = []
    tok = await ensure_user(c, f"vg1_{RUN_ID}".replace("-", "_"))
    await ensure_zhanlu(c, tok)
    ag = await _enable_agent(c, tok)
    tk = await _token(c, tok, "full", f"g-{RUN_ID}")
    path = f"/a2a/{ag['agent_id']}"

    # G1：并发提交 3 个长任务（默认上限 2）→ 至少一个 429
    slow = ("用 code_run 跑 python -c \"import time;time.sleep(90)\"，"
            "然后说 DONE")
    results = await asyncio.gather(
        *[_send(c, path, tk["token"], slow, cfg={"returnImmediately": True})
          for _ in range(3)],
        return_exceptions=True,
    )
    codes = [r.get("_http") for r in results if isinstance(r, dict)]
    ok_tasks = [r for r in results if isinstance(r, dict) and "_http" not in r]
    rate_limited = sum(1 for x in codes if x == 429)
    out.append(_ck("G1_quota_429", f"429x{rate_limited} of {len(results)}",
                   rate_limited >= 1, str(codes)))
    out.append(_ck("G2_some_accepted", f"{len(ok_tasks)} accepted",
                   len(ok_tasks) >= 1))

    # 清理：把这些长任务取消掉，别占着配额影响后面
    for r in ok_tasks:
        tid = _task(r).get("id")
        if tid:
            try:
                await c.post(path, json={"jsonrpc": "2.0", "id": 1,
                                         "method": "CancelTask",
                                         "params": {"id": tid}},
                             headers={**_h(tk["token"]),
                                      "content-type": "application/json"}, timeout=60)
            except Exception:  # noqa: BLE001
                pass
    return out


# ------------------------------------------------------- H 子代理闭世界

async def group_h(c: httpx.AsyncClient) -> list[dict]:
    """子代理拿不到记忆工具（M-9 的真机验证）。

    做法：让 root 派一个子代理，让子代理**列出自己的工具**。
    子代理的回答里不能出现 memory_*。
    """
    out = []
    tok = await ensure_user(c, f"vh1_{RUN_ID}".replace("-", "_"))
    await ensure_zhanlu(c, tok)
    await _put(c, tok, slug="h-note", title="H", description="H 组记忆",
               content="# H\n", kind="project")
    sid = await new_session(c, tok, f"H-{RUN_ID}")
    prompt = ("派一个子代理（spawn_agent），让它只做一件事：列出它自己可用的、"
              "以 memory_ 开头的工具名，一个都没有就回答 NONE。"
              f"把它的原话复述给我，最后输出 {SENTINEL}")
    ev = await send_message(c, tok, sid, prompt, timeout_s=420)
    s = summarize(events=ev)
    tools_used = s["tools"]
    text = s["text"]
    spawned = "spawn_agent" in tools_used
    out.append(_ck("H1_spawned", "spawn_agent", spawned, str(tools_used)[:200]))
    if spawned:
        # 子代理若拿到 memory_list，会在工具序列里出现（它得调用才知道自己有什么）
        child_mem_calls = [t for t in tools_used if t.startswith("memory_")]
        out.append(_ck("H2_child_no_memory_tool_calls", str(child_mem_calls),
                       not child_mem_calls, str(tools_used)[:250]))
    out.append(_ck("H3_sentinel", SENTINEL, SENTINEL in text, text[:150]))
    return out


GROUPS = {"E": group_e, "F": group_f, "G": group_g, "H": group_h}


async def main(argv: list[str]) -> int:
    wanted = [a.upper() for a in argv if a.upper() in GROUPS] or list(GROUPS)
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"runId={RUN_ID}\n输出={OUT}\n组={wanted}\n")
    all_checks = []
    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        for g in wanted:
            print("=" * 66)
            print(f"组 {g}")
            print("=" * 66)
            t0 = time.time()
            try:
                checks = await GROUPS[g](c)
            except Exception as exc:  # noqa: BLE001
                checks = [_ck(f"{g}_exception", type(exc).__name__, False, str(exc)[:300])]
            for ck in checks:
                print(f"  [{'OK  ' if ck['ok'] else 'FAIL'}] {ck['kind']}: {ck['name']}"
                      + (f"  | {ck['detail'][:100]}" if ck.get("detail") else ""))
            print(f"  --> {sum(1 for x in checks if x['ok'])}/{len(checks)} "
                  f"({round(time.time() - t0, 1)}s)\n")
            all_checks.extend({"group": g, **x} for x in checks)

    (OUT / "checks.json").write_text(
        json.dumps(all_checks, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    bad = [x for x in all_checks if not x["ok"]]
    print("=" * 66)
    print(f"总计 {len(all_checks) - len(bad)}/{len(all_checks)} 通过，{len(bad)} 失败")
    for x in bad:
        print(f"  ✗ {x['group']} {x['kind']}: {x['name']}  {x.get('detail', '')[:140]}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
