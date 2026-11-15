"""构造实测：重启到底会丢什么（只读，不删数据）。**

要验证三件事，不能靠读代码猜：

| 场景 | 期望（如果持久化做得好） | 我怀疑的实际 |
|---|---|---|
| R1 A2A 任务跑到一半重启 | 任务继续或至少可查进度 | 被标 FAILED，进度全丢 |
| R2 网页 SSE 断线后重连 | 续传或至少能看到已生成内容 | 内容在（250ms 落库），但事件流断了无法续 |
| R3 子代理跑到一半重启 | 子代理状态可查 | 被标 errored |
| R4 生成中内容 | 保留 | 保留（每 250ms 落库）—— 这条是好的 |

做法：起一个长任务 -> 杀掉后端 -> 起新后端 -> 查状态。

**注意**：本脚本不删任何数据，只在测试用户下新建记录。
"""
from __future__ import annotations

import json
import subprocess
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


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def main() -> int:
    from tests.e2e.harness import ensure_user, ensure_zhanlu

    results: list[dict] = []
    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        tok = await ensure_user(c, f"pd_{RID}".replace("-", "_"))
        await ensure_zhanlu(c, tok)
        r = await c.post("/api/a2a/agent/enabled", json={"enabled": True}, headers=_h(tok))
        r.raise_for_status()
        ag = (await c.get("/api/a2a/agent", headers=_h(tok))).json()
        tk = (await c.post("/api/a2a/tokens",
                           json={"name": f"pd-{RID}", "tool_policy": "full",
                                 "expires_in_days": 1}, headers=_h(tok))).json()
        path = f"/a2a/{ag['agent_id']}"
        a2a_tok = tk["token"]

        # ---- R1: 提交一个长任务，不等它完成 ----
        print("提交一个长任务（90 秒）…")
        body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
            "message": {"messageId": f"pd_{uuid.uuid4().hex[:10]}", "role": "ROLE_USER",
                        "parts": [{"text": "用 code_run 跑 "
                                           "python -c \"import time;time.sleep(90);print('FINISHED')\"，"
                                           "然后复述输出"}]},
            "configuration": {"returnImmediately": True}}}
        r = await c.post(path, json=body,
                         headers={**_h(a2a_tok), "content-type": "application/json"},
                         timeout=60)
        a2a_task = ((r.json().get("result") or {}).get("task") or {})
        a2a_tid = a2a_task.get("id", "")
        print(f"  task_id = {a2a_tid}")

        # ---- R2: 开一个网页会话，发一条会跑很久的消息（SSE 在另一个连接里） ----
        sid = (await c.post("/api/sessions", json={"title": f"PD-{RID}"},
                            headers=_h(tok))).json()["id"]

        print(f"  网页 session = {sid}")

        # 必须真的发一条会跑很久的消息 —— 只建会话不发，重启后自然是 0 条，
        # 那是探针的 bug 而不是产品行为（第一版就踩了这个）。
        import asyncio as _aio

        async def _fire_and_forget():
            try:
                async with c.stream("POST", f"/api/sessions/{sid}/messages",
                                    json={"text": "用 code_run 跑 "
                                                  "python -c \"import time;time.sleep(90);print('WEB_DONE')\"，"
                                                  "然后复述输出"},
                                    headers={**_h(tok), "Accept": "text/event-stream"},
                                    timeout=200) as resp:
                    async for _ in resp.aiter_lines():
                        pass
            except Exception:  # noqa: BLE001 - 我们要的就是中途被打断
                pass

        web_task = _aio.create_task(_fire_and_forget())
        print("  已发出网页消息（后台等待中）")
        print("等 12 秒，让两边都真的开始跑…")
        # ⚠️ 必须用 asyncio.sleep：time.sleep 会阻塞事件循环，上面那个
        # fire-and-forget 任务根本没机会跑，assistant 行也就不会落库 ——
        # 第一版踩过这个坑，误判成「页面侧丢失」。
        await _aio.sleep(12)

        # 记录重启前状态
        before_a2a = (await c.post(path, json={"jsonrpc": "2.0", "id": 1,
                                               "method": "GetTask",
                                               "params": {"id": a2a_tid}},
                                   headers={**_h(a2a_tok),
                                            "content-type": "application/json"},
                                   timeout=60)).json()
        st_before = ((before_a2a.get("result") or {}).get("status") or {}).get("state")
        print(f"  重启前 A2A 状态 = {st_before}")

        (ROOT / "work" / "pd_state.json").write_text(json.dumps({
            "a2a_tid": a2a_tid, "agent_id": ag["agent_id"], "a2a_token": a2a_tok,
            "session_id": sid, "user_token": tok, "rid": RID,
        }, ensure_ascii=False), encoding="utf-8", newline="\n")

        results.append({"check": "R0_提交成功", "ok": st_before in
                        ("TASK_STATE_WORKING", "TASK_STATE_SUBMITTED"),
                        "detail": st_before})

    print()
    print("=" * 62)
    print("现在需要重启后端。请手动执行，然后跑第二阶段：")
    print("  1) 杀后端进程")
    print("  2) python tools/persistence_verify/launch_verify.py")
    print("  3) cd backend && python ../tools/persistence_verify/probe_persistence.py after")
    print("=" * 62)
    (ROOT / "work" / "pd_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return 0


async def after() -> int:
    from tests.e2e.harness import BASE as B  # noqa: F401
    state = json.loads((ROOT / "work" / "pd_state.json").read_text(encoding="utf-8"))
    out: list[dict] = []
    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        path = f"/a2a/{state['agent_id']}"
        got = (await c.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "GetTask",
                                        "params": {"id": state["a2a_tid"]}},
                            headers={**_h(state["a2a_token"]),
                                     "content-type": "application/json"},
                            timeout=60)).json()
        res = got.get("result") or {}
        st = (res.get("status") or {}).get("state", "")
        err = ""
        for p in ((res.get("status") or {}).get("message") or {}).get("parts") or []:
            err += str(p.get("text") or "")
        arts = "".join(str(p.get("text") or "")
                       for a in (res.get("artifacts") or [])
                       for p in (a.get("parts") or []) if isinstance(p, dict))
        out.append({"check": "R1_A2A重启后不丢现场",
                    "ok": st == "TASK_STATE_FAILED" and "中断" in err
                          and ("第" in err or "没有进展记录" in err),
                    "detail": f"state={st} err={err[:200]}"})

        msgs = (await c.get(f"/api/sessions/{state['session_id']}/messages",
                            headers=_h(state["user_token"]))).json()
        out.append({"check": "R3_网页消息是否保留", "ok": len(msgs) > 0,
                    "detail": f"{len(msgs)} 条"})
        for m in msgs:
            out.append({"check": f"  消息[{m.get('role')}].status",
                        "ok": True,
                        "detail": f"status={m.get('status')} "
                                  f"content={(m.get('content') or '')[:60]}"})

    print("=" * 62)
    for x in out:
        print(f"  [{'OK  ' if x['ok'] else 'FAIL'}] {x['check']}: {x['detail'][:140]}")
    print("=" * 62)
    (ROOT / "work" / "pd_after.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "after":
        sys.exit(__import__("asyncio").run(after()))
    sys.exit(__import__("asyncio").run(main()))
