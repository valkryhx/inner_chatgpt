"""诊断 A2A 并发配额：是「完全没生效」还是「并发竞态 TOCTOU」？（只读）

G1 实测：3 个并发提交全部被接受，没有 429。两种可能：

  (a) 配额判定根本没跑 / 不生效  —— 串行提交也不会 429
  (b) TOCTOU 竞态              —— 串行会 429，并发不会

`_prepare_task` 的顺序是「先 count_active 再 create_task」，中间有 await，
所以理论上三个并发请求都能看到 count=0。这个脚本把两者分开测。

跑法::

    cd backend && python ../tools/a2a_verify/quota_probe.py
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402
from tests.e2e.harness import BASE, ensure_user, ensure_zhanlu  # noqa: E402

RUN_ID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _agent(c, tok):
    r = await c.post("/api/a2a/agent/enabled", json={"enabled": True}, headers=_h(tok))
    r.raise_for_status()
    r = await c.get("/api/a2a/agent", headers=_h(tok))
    r.raise_for_status()
    return r.json()


async def _token(c, tok, name):
    r = await c.post("/api/a2a/tokens",
                     json={"name": name, "tool_policy": "full", "expires_in_days": 1},
                     headers=_h(tok))
    r.raise_for_status()
    return r.json()


async def _submit(c, path, tok):
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
        "message": {"messageId": f"q_{uuid.uuid4().hex[:10]}", "role": "ROLE_USER",
                    "parts": [{"text": "用 code_run 跑 python -c \"import time;time.sleep(120)\""}]},
        "configuration": {"returnImmediately": True}}}
    r = await c.post(path, json=body,
                     headers={**_h(tok), "content-type": "application/json"}, timeout=120)
    return r.status_code, (r.text[:160] if r.status_code >= 400 else "ok")


async def _cancel(c, path, tok, tid):
    try:
        await c.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "CancelTask",
                                 "params": {"id": tid}},
                     headers={**_h(tok), "content-type": "application/json"}, timeout=60)
    except Exception:  # noqa: BLE001
        pass


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        tok = await ensure_user(c, f"pq_{RUN_ID}".replace("-", "_"))
        await ensure_zhanlu(c, tok)
        ag = await _agent(c, tok)
        tk = await _token(c, tok, f"pq-{RUN_ID}")
        path = f"/a2a/{ag['agent_id']}"

        print("=" * 64)
        print("串行提交 3 个长任务（默认上限 2）")
        print("=" * 64)
        ids = []
        for i in range(3):
            code, body = await _submit(c, path, tk["token"])
            print(f"  第 {i + 1} 个: HTTP {code}  {body[:100]}")
            if code == 200:
                import json as _j
                try:
                    ids.append(_j.loads(body if body != "ok" else "{}").get("result", {})
                               .get("task", {}).get("id", ""))
                except Exception:  # noqa: BLE001
                    pass
        # 上面 body 被截断了取不到 id，改用 ListTasks 清理
        got = await c.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks",
                                       "params": {}},
                           headers={**_h(tk["token"]), "content-type": "application/json"},
                           timeout=60)
        for t in ((got.json().get("result") or {}).get("tasks") or []):
            await _cancel(c, path, tk["token"], t.get("id"))

        await asyncio.sleep(3)
        print()
        print("=" * 64)
        print("并发提交 3 个长任务（新用户，干净配额）")
        print("=" * 64)
        tok2 = await ensure_user(c, f"pq2_{RUN_ID}".replace("-", "_"))
        await ensure_zhanlu(c, tok2)
        ag2 = await _agent(c, tok2)
        tk2 = await _token(c, tok2, f"pq2-{RUN_ID}")
        path2 = f"/a2a/{ag2['agent_id']}"
        res = await asyncio.gather(*[_submit(c, path2, tk2["token"]) for _ in range(3)],
                                   return_exceptions=True)
        for i, r in enumerate(res, 1):
            if isinstance(r, Exception):
                print(f"  第 {i} 个: EXC {type(r).__name__}")
            else:
                print(f"  第 {i} 个: HTTP {r[0]}  {r[1][:100]}")
        got2 = await c.post(path2, json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks",
                                         "params": {}},
                            headers={**_h(tk2["token"]),
                                     "content-type": "application/json"}, timeout=60)
        for t in ((got2.json().get("result") or {}).get("tasks") or []):
            await _cancel(c, path2, tk2["token"], t.get("id"))

        print()
        print("=" * 64)
        print("判据")
        print("=" * 64)
        print("  串行出现 429 → 配额逻辑本身是好的，问题在并发竞态（TOCTOU）")
        print("  串行也没 429 → 配额根本没生效（读不到活跃数 / 上限读错）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
