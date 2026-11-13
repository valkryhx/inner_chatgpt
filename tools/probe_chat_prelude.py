"""探针 A：测量 chat SSE 首帧与各阶段耗时（常规场景）。

用途：验证「首帧前有串行阻塞」，并给出 `message_start` / 首个 token 的真实时刻。
见 docs/20261112-Hlaudz首帧串行阻塞核实与优化设计.md §5.1。

用法（后端需在跑）：
    python tools/probe_chat_prelude.py
    HLZ_BASE=http://127.0.0.1:8000 HLZ_TOKEN=<access_token> python tools/probe_chat_prelude.py

token 怎么拿：本机开发环境若不便登录，可用项目自己的 auth 签发：
    python -c "
    import sys, asyncio; sys.path.insert(0,'.')
    from backend.app import auth, db
    async def m():
        u = await db.get_user('<user_id>')
        t,_ = auth._make_token(u['id'], int(u.get('token_ver',0)), 'access', auth.ACCESS_TTL)
        print(t)
    asyncio.run(m())"
"""
import asyncio
import os
import sys
import time
import uuid

import httpx

BASE = os.environ.get("HLZ_BASE", "http://127.0.0.1:8000")
EMAIL = os.environ.get("HLZ_EMAIL", "")
PASSWORD = os.environ.get("HLZ_PASSWORD", "")
TOKEN = os.environ.get("HLZ_TOKEN", "")

_TEXT = os.environ.get("HLZ_PROBE_TEXT", "请用一句话回答：1+1 等于几。")


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=300.0) as c:
        token = TOKEN
        if not token and EMAIL and PASSWORD:
            r = await c.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
            if r.status_code == 200:
                token = r.json().get("access_token", "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        print("token ok" if token else "NO TOKEN")

        r = await c.post("/api/sessions", json={"title": "probe-prelude"}, headers=headers)
        if r.status_code not in (200, 201):
            print("建会话失败", r.status_code, r.text[:300])
            return 1
        sid = r.json()["id"]
        print(f"session={sid}")

        marks: list[tuple[float, str]] = []
        t0 = time.monotonic()
        payload = {"text": _TEXT, "generation_id": uuid.uuid4().hex}
        async with c.stream("POST", f"/api/sessions/{sid}/messages",
                            json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                print("流式请求失败", resp.status_code, body[:400])
                return 1
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

        print("\n阶段耗时（ms）:")
        prev = 0.0
        seen: set[str] = set()
        for elapsed, name in marks:
            if name in seen and name in ("text_delta", "thinking_delta"):
                continue
            seen.add(name)
            print(f"  {elapsed:8.1f}  (+{elapsed - prev:7.1f})  {name}")
            prev = elapsed

        def first(n: str) -> float | None:
            return next((t for t, x in marks if x == n), None)

        print(f"\n首帧(message_start)  = {first('message_start')} ms")
        print(f"首个 thinking        = {first('thinking_delta')} ms")
        print(f"首个正文 text_delta   = {first('text_delta')} ms")
        print(f"整轮结束              = {marks[-1][0] if marks else 0:.1f} ms")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
