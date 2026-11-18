"""验证 `/stop` 能级联到容器内进程（设计文档 §6.5）。

为什么必须单独测：宿主 ``process_registry._kill_tree`` 只管宿主进程，
**容器内进程不受它管辖**。不做这条级联，「停止」在沙箱里就是假的。

判定不靠「接口返回 ok」：接口永远返回 ok。
要证明的是：容器内那个 sleep 进程**真的没了**。
"""
from __future__ import annotations

import asyncio
import secrets
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000"
HX = "u_c56b8337dd09"


async def token_for(uid: str) -> str:
    from app import db
    from app.auth import create_access_token
    u = await db.get_user(uid)
    t, _ = create_access_token(uid, u.get("token_ver", 0))
    return t


def container_pids(name: str) -> str:
    """容器内长跑 python 进程数。

    ⚠️ 不能用 ``ps``：``python:3.12-slim`` 没有 procps（实测 which 空）。
    用 /proc 遍历，只有 argv 以 /ws/ 开头的才算业务进程。
    """
    import subprocess
    script = (
        "import os\n"
        "me = os.getpid()\n"
        "n = 0\n"
        "for pid in os.listdir('/proc'):\n"
        "    if not pid.isdigit() or int(pid) == me:\n"
        "        continue\n"
        "    try:\n"
        "        parts = open('/proc/' + pid + '/cmdline','rb').read().split(b'\\x00')\n"
        "    except OSError:\n"
        "        continue\n"
        "    if any(p.startswith(b'/ws/') for p in parts):\n"
        "        n += 1\n"
        "print(n, flush=True)\n"
    )
    r = subprocess.run(["docker", "exec", name, "python", "-c", script],
                       capture_output=True, text=True, timeout=25)
    return (r.stdout or r.stderr).strip()


async def main() -> int:
    import subprocess

    token = await token_for(HX)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=BASE, timeout=None) as client:
        r = await client.post("/api/sessions", json={"title": "stop-cascade"},
                              headers=headers)
        r.raise_for_status()
        sid = r.json()["id"]

        # 让模型跑一个 300 秒的 sleep，然后我们从外面停
        prompt = ("请用 code_run 执行这段代码：\n"
                  "import time\n"
                  "print('LONG_STARTED')\n"
                  "time.sleep(300)\n"
                  "print('LONG_DONE')\n"
                  "执行完把输出告诉我。")
        print(f"session={sid}")
        print("发送长跑任务…")

        gen_id = None

        async def consume():
            nonlocal gen_id
            async with client.stream("POST", f"/api/sessions/{sid}/messages",
                                     json={"text": prompt}, headers=headers,
                                     timeout=600) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        raw = line[6:].strip()
                        if raw and raw != "[DONE]":
                            try:
                                import json
                                e = json.loads(raw)
                                if e.get("generation_id"):
                                    gen_id = e["generation_id"]
                            except Exception:
                                pass

        task = asyncio.create_task(consume())

        # 等容器内出现长跑进程
        pids_before = ""
        for _ in range(60):
            await asyncio.sleep(2)
            pids_before = container_pids("hlz_sbx_u_c56b8337dd09")
            if pids_before.isdigit() and int(pids_before) > 0:
                break
        print(f"停止前，容器内 python 进程数 = {pids_before}")
        if not (pids_before.isdigit() and int(pids_before) > 0):
            print("FAIL: 长跑进程没起来，测不了级联")
            task.cancel()
            return 1

        # 发 /stop
        r = await client.post(f"/api/sessions/{sid}/stop",
                              json={"generation_id": gen_id} if gen_id else {},
                              headers=headers)
        print(f"stop 响应 {r.status_code}: {r.text[:150]}")

        # 等进程消失
        gone = False
        for _ in range(30):
            await asyncio.sleep(1)
            after = container_pids("hlz_sbx_u_c56b8337dd09")
            if after.isdigit() and int(after) == 0:
                gone = True
                break
        print(f"停止后，容器内 python 进程数 = {after if 'after' in dir() else '?'}")

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    print("\n== 判定 ==")
    if gone:
        print("  [PASS] /stop 级联到了容器内进程")
        return 0
    print("  [FAIL] 容器内进程仍在跑 —— 级联没生效")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))