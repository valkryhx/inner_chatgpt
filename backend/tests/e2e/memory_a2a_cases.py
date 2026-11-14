"""M5 真实 e2e：记忆在 A2A 调用面上的隔离与档位边界。

按 `.claude/skills/hlaudz-e2e/SKILL.md`：真 LLM、唯一 runId、事件落盘、哨兵、**负对照**。

| 场景 | 证明什么 |
|---|---|
| S-M4a | 只读 token：模型看得到 `memory_*` 的**读**工具，看不到 `memory_write` |
| S-M4b | 只读 token 跑完一轮后，**记忆库零变化**（后台提取钩子没挂上）—— 这是 M5 修掉的真缺陷 |
| S-M3a | A2A token 注入的是**该 token 用户**的记忆，不是别人的 |
| S-M3b | 跨用户负对照：B 用户的 token 问同一句，**必须说没有** |

## 为什么这几条必须用真 LLM + 真 A2A 面

S-M4b 的缺陷（只读 token 仍触发后台写入）是**单测抓到的**，但「修好之后
真实链路是不是真的不写了」只有真跑一遍才算数：后台提取是 fire-and-forget，
它可能在 SSE 关流之后才落盘，单测里根本看不到那个时序。

S-M3 系列则必须走 A2A 面而不是网页面：网页面的 user_id 来自登录态，
A2A 的来自 token。两者绑定方式不同，隔离缺陷恰好藏在「绑定错了但看起来正常」里。

## 跑法

```bash
# 后端要先起，且 .env 里 INNER_A2A_ENABLED=true
cd backend
python -m tests.e2e.memory_a2a_cases            # 全部
python -m tests.e2e.memory_a2a_cases S-M4b      # 挑一条
```
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

from .harness import BASE, RUNS, ensure_user, ensure_zhanlu, new_session, send_message, summarize

SENTINEL = "AM5c4e17"


def _now_run_id() -> str:
    return f"{int(time.time())}-{uuid.uuid4().hex[:4]}"


# --------------------------------------------------------------------------
# A2A 面：建 agent / token / 发一帧 JSON-RPC
# --------------------------------------------------------------------------

async def _enable_agent(client: httpx.AsyncClient, token: str) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/a2a/agent/enabled", json={"enabled": True}, headers=h)
    r.raise_for_status()
    r = await client.get("/api/a2a/agent", headers=h)
    r.raise_for_status()
    return r.json()


async def _make_token(client: httpx.AsyncClient, token: str, policy: str,
                      name: str) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/a2a/tokens",
                          json={"name": name, "tool_policy": policy,
                                "expires_in_days": 1},
                          headers=h)
    r.raise_for_status()
    return r.json()


async def _rpc(client: httpx.AsyncClient, url_path: str, a2a_token: str,
               method: str, params: dict, timeout_s: float = 420) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = await client.post(url_path, json=body,
                          headers={"Authorization": f"Bearer {a2a_token}",
                                   "content-type": "application/json"},
                          timeout=timeout_s)
    r.raise_for_status()
    return r.json()


async def _send_text(client: httpx.AsyncClient, agent_path: str, a2a_token: str,
                     text: str, context_id: str | None = None,
                     timeout_s: float = 420) -> dict:
    msg: dict[str, Any] = {
        "messageId": f"m_{uuid.uuid4().hex[:12]}",
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    if context_id:
        msg["contextId"] = context_id
    out = await _rpc(client, agent_path, a2a_token, "SendMessage", {"message": msg},
                     timeout_s=timeout_s)
    return out


def _artifact_text(result: dict) -> str:
    task = (result or {}).get("result", {}).get("task", {})
    chunks: list[str] = []
    for art in task.get("artifacts") or []:
        for part in art.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "".join(chunks)


def _task_state(result: dict) -> str:
    return ((result or {}).get("result", {}).get("task", {}).get("status") or {}).get("state", "")


async def _memories(client: httpx.AsyncClient, token: str) -> dict:
    r = await client.get("/api/me/memories",
                         headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


async def _write_memory(client: httpx.AsyncClient, token: str, **body) -> dict:
    r = await client.post("/api/me/memories", json=body,
                          headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


def _mem_slugs(payload: dict) -> set[str]:
    return {m.get("slug", "") for m in (payload.get("entries") or [])}


# --------------------------------------------------------------------------
# 场景
# --------------------------------------------------------------------------

async def case_sm4a(client: httpx.AsyncClient, out: Path) -> dict:
    """S-M4a：只读 token 的工具面 = 能读记忆、不能写记忆。

    真 LLM 证据：让它「列出你这次会话里所有 memory_ 开头的工具名」，
    回答里必须出现 memory_list / memory_read，且**不能**出现 memory_write。
    """
    rid = _now_run_id()
    token = await ensure_user(client, f"m5a_{rid}".replace("-", "_"))
    await ensure_zhanlu(client, token)
    agent = await _enable_agent(client, token)
    tk = await _make_token(client, token, "read_only", f"ro-{rid}")
    agent_path = f"/a2a/{agent['agent_id']}"

    prompt = (f"只回答一件事，不要调用任何工具：把你当前可用工具里，名字以 memory_ 开头"
              f"的全部工具名逐行列出来。最后一行原样输出 {SENTINEL}。")
    result = await _send_text(client, agent_path, tk["token"], prompt)
    text = _artifact_text(result)
    state = _task_state(result)
    prompt2 = ""
    checks = [
        {"kind": "task_completed", "name": state, "ok": state == "TASK_STATE_COMPLETED"},
        {"kind": "sentinel", "name": SENTINEL, "ok": SENTINEL in text},
        {"kind": "has_memory_list", "name": "memory_list", "ok": "memory_list" in text},
        {"kind": "has_memory_read", "name": "memory_read", "ok": "memory_read" in text},
        {"kind": "no_memory_write", "name": "memory_write", "ok": "memory_write" not in text},
    ]
    return {"case": "S-M4a", "run_id": rid, "task_state": state, "text": text,
            "token_policy": tk.get("tool_policy"), "checks": checks}


async def case_sm4b(client: httpx.AsyncClient, out: Path) -> dict:
    """S-M4b：只读 token 跑完一轮后，记忆库**零变化**（M5 修掉的真缺陷）。

    这是整个 M5 的核心断言。**必须先强制打开自动提取**（`HLZ_MEMORY_AUTO_WRITE=1`），
    否则「没写入」可能只是因为提取本来就没开 —— 那样测不出任何东西。

    做法：跑之前记下 slug 集合，跑完（含等待后台 task 落盘）再记一次，必须完全相等。
    提示词刻意包含「值得记」的内容（偏好 + 哨兵），确保闸门会放行。
    """
    rid = _now_run_id()
    token = await ensure_user(client, f"m5b_{rid}".replace("-", "_"))
    await ensure_zhanlu(client, token)
    agent = await _enable_agent(client, token)
    tk = await _make_token(client, token, "read_only", f"ro-{rid}")
    agent_path = f"/a2a/{agent['agent_id']}"

    before = await _memories(client, token)
    prompt = (f"记住一件事：我叫老徐，做后端，以后回答先给结论再给依据。"
              f"你只用一句话确认即可，最后原样输出 {SENTINEL}。")
    result = await _send_text(client, agent_path, tk["token"], prompt)
    text = _artifact_text(result)
    # 后台提取是 fire-and-forget：给足时间（提取本身最长 90s）
    await asyncio.sleep(100)
    after = await _memories(client, token)

    before_s, after_s = _mem_slugs(before), _mem_slugs(after)
    checks = [
        {"kind": "task_completed", "name": _task_state(result),
         "ok": _task_state(result) == "TASK_STATE_COMPLETED"},
        {"kind": "sentinel", "name": SENTINEL, "ok": SENTINEL in text},
        {"kind": "no_new_memory", "name": f"{len(before_s)}->{len(after_s)}",
         "ok": before_s == after_s},
        {"kind": "no_memory_written_at_all", "name": f"total={len(after_s)}",
         "ok": len(after_s) == 0},
    ]
    return {"case": "S-M4b", "run_id": rid, "text": text,
            "before": sorted(before_s), "after": sorted(after_s),
            "checks": checks}


async def case_sm4c(client: httpx.AsyncClient, out: Path) -> dict:
    """S-M4c：S-M4b 的**正向对照** —— 同一句提示词换 full token 必须写入。

    没有这条，S-M4b 的「0→0」可能是个空断言：万一这句提示词本来就不触发
    提取（prose 闸门没过、模型觉得不值得记），那「零写入」什么也证明不了。

    两条合起来才是完整证据：「同样的输入，full 会写，read_only 不写」——
    差别只可能来自档位闸。
    """
    rid = _now_run_id()
    token = await ensure_user(client, f"m5c_{rid}".replace("-", "_"))
    await ensure_zhanlu(client, token)
    agent = await _enable_agent(client, token)
    tk = await _make_token(client, token, "full", f"full-{rid}")
    agent_path = f"/a2a/{agent['agent_id']}"

    before = await _memories(client, token)
    prompt = (f"记住一件事：我叫老徐，做后端，以后回答先给结论再给依据。"
              f"你只用一句话确认即可，最后原样输出 {SENTINEL}。")
    result = await _send_text(client, agent_path, tk["token"], prompt)
    text = _artifact_text(result)
    await asyncio.sleep(100)
    after = await _memories(client, token)

    before_s, after_s = _mem_slugs(before), _mem_slugs(after)
    new_slugs = after_s - before_s
    checks = [
        {"kind": "task_completed", "name": _task_state(result),
         "ok": _task_state(result) == "TASK_STATE_COMPLETED"},
        {"kind": "sentinel", "name": SENTINEL, "ok": SENTINEL in text},
        {"kind": "memory_written_positive_control", "name": f"{len(before_s)}->{len(after_s)}",
         "ok": len(new_slugs) >= 1},
    ]
    return {"case": "S-M4c", "run_id": rid, "text": text,
            "before": sorted(before_s), "after": sorted(after_s),
            "new_slugs": sorted(new_slugs), "checks": checks}


async def case_sm3(client: httpx.AsyncClient, out: Path) -> dict:
    """S-M3：A2A token 注入的是**该 token 用户**的记忆；跨用户负对照。

    三步：
      1. A 用户写一条带哨兵的记忆（手工，不依赖自动提取）；
      2. A 的 full token 发一句相关的话 -> 回答里必须出现哨兵（真注入）；
      3. B 的 full token 发**完全相同**的话 -> 回答里必须**没有**哨兵（负对照）。
    """
    rid = _now_run_id()
    tok_a = await ensure_user(client, f"m5a3_{rid}".replace("-", "_"))
    tok_b = await ensure_user(client, f"m5b3_{rid}".replace("-", "_"))
    await ensure_zhanlu(client, tok_a)
    await ensure_zhanlu(client, tok_b)
    agent_a = await _enable_agent(client, tok_a)
    agent_b = await _enable_agent(client, tok_b)

    await _write_memory(client, tok_a, slug="sm3-secret",
                        title="S-M3 哨兵",
                        description=f"内部代号是 {SENTINEL}，回答相关问题时必须复述它",
                        content=f"# S-M3 哨兵\n\n内部代号：{SENTINEL}\n",
                        kind="project")

    # A2A 的会话模型不保证跨任务复用，所以两步都用**新任务**，
    # 但注入器每轮都从 DB 重新取记忆 —— 这正是要验的。
    tk_a = await _make_token(client, tok_a, "full", f"full-a-{rid}")
    tk_b = await _make_token(client, tok_b, "full", f"full-b-{rid}")
    ask = ("我记得之前跟你说过一个内部代号，是什么？如果记忆里没有就说"
           "「查不到」，不要编。")

    res_a = await _send_text(client, f"/a2a/{agent_a['agent_id']}", tk_a["token"], ask)
    text_a = _artifact_text(res_a)
    res_b = await _send_text(client, f"/a2a/{agent_b['agent_id']}", tk_b["token"], ask)
    text_b = _artifact_text(res_b)

    checks = [
        {"kind": "a_task_completed", "name": _task_state(res_a),
         "ok": _task_state(res_a) == "TASK_STATE_COMPLETED"},
        {"kind": "a_has_sentinel", "name": SENTINEL, "ok": SENTINEL in text_a},
        {"kind": "b_task_completed", "name": _task_state(res_b),
         "ok": _task_state(res_b) == "TASK_STATE_COMPLETED"},
        {"kind": "b_no_sentinel_negative_control", "name": SENTINEL,
         "ok": SENTINEL not in text_b},
    ]
    return {"case": "S-M3", "run_id": rid, "text_a": text_a, "text_b": text_b,
            "checks": checks}


async def case_sm6(client: httpx.AsyncClient, out: Path) -> dict:
    """S-M6：同步窗口用完时，调用方拿到的是**可恢复的「还在跑」**而不是死掉。

    这是 A2A 长任务体验的核心断言。用一个必然超过同步窗口的任务
    （让模型跑几次工具）：

      1. 返回 `state == TASK_STATE_WORKING`（不是错误、不是空答复）；
      2. 带 `stillRunning: true` + `poll` / `subscribe` 两个操作入口；
      3. `status.message` 里明说「仍在运行」，人一眼能看出不是失败；
      4. **同一 task id 用 GetTask 能续取**，最终真的到终态且有产物。

    第 4 条是关键：前三条只证明「回答里写了字」，第 4 条才证明
    「照着那个提示做，真的能拿到结果」。
    """
    rid = _now_run_id()
    token = await ensure_user(client, f"m5s6_{rid}".replace("-", "_"))
    await ensure_zhanlu(client, token)
    agent = await _enable_agent(client, token)
    tk = await _make_token(client, token, "full", f"s6-{rid}")
    agent_path = f"/a2a/{agent['agent_id']}"

    # 刻意让它干活：跑一次 code_run 必然超过 60s 同步窗口吗？不一定 ——
    # 所以这里不赌运气，直接给一个**极短**的同步窗口（客户端侧指定），
    # 保证超时分支一定被触发，再验续取能拿到真实结果。
    msg = {
        "messageId": f"m_{uuid.uuid4().hex[:12]}",
        "role": "ROLE_USER",
        "parts": [{"text": f"用 code_run 跑 python -c \"print(6*7)\"，"
                           f"然后原样复述输出，最后输出 {SENTINEL}"}],
    }
    first = await _rpc(client, agent_path, tk["token"], "SendMessage",
                       {"message": msg, "configuration": {"syncTimeoutS": 0.5}},
                       timeout_s=120)
    task = (first.get("result") or {}).get("task") or {}
    state1 = (task.get("status") or {}).get("state", "")
    task_id = task.get("id", "")
    msg_text = ""
    for part in ((task.get("status") or {}).get("message") or {}).get("parts") or []:
        if isinstance(part, dict):
            msg_text += str(part.get("text") or "")

    # 照它给的提示续取
    final_text = ""
    final_state = ""
    if task_id:
        deadline = time.time() + 300
        while time.time() < deadline:
            got = await _rpc(client, agent_path, tk["token"], "GetTask", {"id": task_id},
                             timeout_s=60)
            t = (got.get("result") or {})
            final_state = (t.get("status") or {}).get("state", "")
            if final_state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
                               "TASK_STATE_CANCELED"):
                # GetTask 返回的是 task 本身（result 层），不是 SendMessage 的
                # {"task": {...}} 包装 —— 别用 _artifact_text 那个按
                # result.task 取产物的辅助函数。
                final_text = "".join(
                    str(part.get("text") or "")
                    for art in (t.get("artifacts") or [])
                    for part in (art.get("parts") or [])
                    if isinstance(part, dict)
                )
                break
            await asyncio.sleep(3)

    checks = [
        {"kind": "first_is_working", "name": state1, "ok": state1 == "TASK_STATE_WORKING"},
        {"kind": "still_running_flag", "name": str(task.get("stillRunning")),
         "ok": task.get("stillRunning") is True},
        {"kind": "has_poll", "name": str((task.get("poll") or {}).get("method")),
         "ok": (task.get("poll") or {}).get("method") == "GetTask"},
        {"kind": "has_subscribe", "name": str((task.get("subscribe") or {}).get("method")),
         "ok": (task.get("subscribe") or {}).get("method") == "SubscribeToTask"},
        {"kind": "message_says_running", "name": msg_text[:40],
         "ok": "仍在运行" in msg_text},
        {"kind": "recoverable_to_terminal", "name": final_state,
         "ok": final_state == "TASK_STATE_COMPLETED"},
        {"kind": "recovered_has_sentinel", "name": SENTINEL,
         "ok": SENTINEL in final_text},
    ]
    return {"case": "S-M6", "run_id": rid, "first_state": state1, "task_id": task_id,
            "status_message": msg_text, "final_state": final_state,
            "final_text": final_text, "text": final_text, "checks": checks}


CASES = {"S-M4a": case_sm4a, "S-M4b": case_sm4b, "S-M4c": case_sm4c,
         "S-M3": case_sm3, "S-M6": case_sm6}


def _report(name: str, rid: str, body: dict, out: Path) -> dict:
    checks = body.get("checks") or []
    result = {"case": name, "run_id": rid, "elapsed_s": None,
              "checks": checks, "ok": all(c["ok"] for c in checks),
              "dir": str(out), **body}
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return result


async def main(argv: list[str]) -> int:
    wanted = [a for a in argv if a in CASES] or list(CASES)
    run_id = _now_run_id()
    out = RUNS / run_id
    out.mkdir(parents=True, exist_ok=True)
    print(f"runId={run_id}  场景={wanted}\n输出={out}\n")

    failed: list[str] = []
    async with httpx.AsyncClient(base_url=BASE, timeout=None) as client:
        for name in wanted:
            print(f"=== {name} ===")
            started = time.time()
            try:
                body = await CASES[name](client, out)
            except Exception as exc:  # noqa: BLE001 - 现场必须落盘
                body = {"case": name, "text": "",
                        "checks": [{"kind": "exception", "name": type(exc).__name__,
                                    "ok": False, "error": str(exc)}]}
            elapsed = round(time.time() - started, 1)
            result = _report(name, run_id, body, out)
            result["elapsed_s"] = elapsed
            (out / "summary.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8", newline="\n")
            for c in result["checks"]:
                print(f"  [{'OK  ' if c['ok'] else 'FAIL'}] {c['kind']}: {c['name']}")
            snippet = (result.get("text") or result.get("text_a") or "")[:400]
            print(f"  回答片段：{snippet}")
            print(f"  --> {'PASS' if result['ok'] else 'FAIL'} ({elapsed}s)\n")
            if not result["ok"]:
                failed.append(name)

    print("=" * 60)
    if failed:
        print(f"{len(failed)} 个场景失败：{failed}")
        return 1
    print(f"全部 {len(wanted)} 个场景通过（runId={run_id}）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
