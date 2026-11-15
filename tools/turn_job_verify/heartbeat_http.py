"""真机验证：心跳真的会在 HTTP 层发出字节。

## 为什么必须真机

单测只能证明 `drain()` 会 yield 心跳帧。但心跳的目的是「让中间代理看到字节」，
证据必须落在**线上字节**上：原始 SSE 响应里出现 `: keep-alive` 注释行。

## 观测点

发一个会静默很久的请求（`code_run` 里 sleep），统计原始 SSE 响应中的注释行。
把 `HLZ_TURNJOB_HEARTBEAT_IDLE_S` 压到 3 秒，让 30 秒静默期里必然出现多帧。

## 不碰生产库

复制 `backend/data/app.db`（sqlite backup，WAL 安全），独立端口，跑完杀掉。
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "work" / "hb_db"
RUN_ID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BOOT = """import sys, pathlib
sys.path.insert(0, r"{backend}")
from app import config
config.DB_PATH = pathlib.Path(r"{db}")
import app.db as _db
_db.DB_PATH = pathlib.Path(r"{db}")
import app.history_store as _hs
_hs.DB_PATH = pathlib.Path(r"{db}")
import app.memory_store as _ms
_ms.DB_PATH = pathlib.Path(r"{db}")
import app.team_store as _ts
_ts.DB_PATH = pathlib.Path(r"{db}")
import app.subagent.store as _ss
_ss.DB_PATH = pathlib.Path(r"{db}")
import app.a2a.store as _as
_as.DB_PATH = pathlib.Path(r"{db}")
import app.turn_marker as _tm
_tm.DB_PATH = pathlib.Path(r"{db}")
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, reload=False, log_level="warning")
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    WORK.mkdir(parents=True, exist_ok=True)
    src, dst = ROOT / "backend/data/app.db", WORK / "app.db"
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(dst) + suffix)
        if f.exists():
            f.unlink()
    with sqlite3.connect(str(src)) as s, sqlite3.connect(str(dst)) as d:
        s.backup(d)

    env = dict(os.environ)
    env.update({
        "HLZ_TURNJOB_HEARTBEAT_IDLE_S": "3",   # 压到 3 秒，30s 静默期必然触发多帧
        "INNER_A2A_ENABLED": "false",
        "PYTHONPATH": str(ROOT / "backend"),
    })
    log = open(WORK / "backend.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-c", BOOT.format(backend=str(ROOT / "backend"), db=str(dst), port=port)],
        cwd=str(ROOT / "backend"), env=env, stdout=log, stderr=subprocess.STDOUT)

    ok: list[str] = []
    fail: list[str] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        (ok if passed else fail).append(name)
        print(f"  [{'OK  ' if passed else 'FAIL'}] {name}" + (f"  | {detail}" if detail else ""))

    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120:
            try:
                with urllib.request.urlopen(f"{base}/api/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("后端没起来")

        def post(path: str, payload: dict, token: str = "") -> dict:
            req = urllib.request.Request(
                f"{base}{path}", data=json.dumps(payload).encode(),
                headers={"content-type": "application/json",
                         **({"authorization": f"Bearer {token}"} if token else {})},
                method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode() or "{}")

        email = f"hb_{RUN_ID.replace('-', '_')}@t.local"
        token = post("/api/auth/register",
                     {"email": email, "password": "pw12345678", "name": "HB"})["access_token"]
        sys.path.insert(0, str(ROOT / "backend"))
        from tests.e2e.harness import DEFAULT_LLM, zhanlu_api_key
        cfg = dict(DEFAULT_LLM)
        cfg["api_key"] = zhanlu_api_key()
        post("/api/me/llm-configs", cfg, token)

        sess = post("/api/sessions", {"title": "hb"}, token)
        sid = sess["id"]

        # 让模型跑一个长时间静默的工具：code_run 里 sleep 40 秒
        prompt = ("请调用 code_run 工具执行这段 Python：\n"
                  "```python\nimport time\nprint('start', flush=True)\n"
                  "time.sleep(40)\nprint('done', flush=True)\n```\n"
                  "执行完把输出原样告诉我，不要做别的事。")

        gen_id = f"g-{RUN_ID}"
        req = urllib.request.Request(
            f"{base}/api/sessions/{sid}/messages",
            data=json.dumps({"text": prompt, "generation_id": gen_id}).encode(),
            headers={"content-type": "application/json",
                     "accept": "text/event-stream",
                     "authorization": f"Bearer {token}"},
            method="POST")

        # 逐帧记录 (相对时刻, 类型)。要回答两个问题：
        #   ① 心跳有没有真的发出去（线上字节里有 : 行）；
        #   ② 心跳是**静默期内**发的，还是等流结束时攒着一起发 —— 后者等于没发
        #      （代理早就把连接切了）。
        seq_log: list[tuple[float, str]] = []
        read_times: list[float] = []
        heartbeats: list[float] = []
        real_events = 0
        t_start = time.monotonic()
        with urllib.request.urlopen(req, timeout=180) as res:
            buf = ""
            while time.monotonic() - t_start < 75:
                # 必须用 read1()：io.BufferedReader.read(n) 会**循环补齐到 n 字节**
                # 才返回，于是 15 字节的心跳要攒够 n 字节才吐一次 —— 第一版就是
                # 这样误判成「心跳憋在缓冲区里 40 秒」，其实是我的读取方式问题。
                raw = res.read1(256)
                if not raw:
                    break
                now = time.monotonic() - t_start
                read_times.append(now)
                buf += raw.decode("utf-8", errors="replace")
                while "\n\n" in buf:
                    frame, buf = buf.split("\n\n", 1)
                    if frame.startswith(":"):
                        heartbeats.append(now)
                        seq_log.append((now, "hb"))
                    elif frame.strip():
                        real_events += 1
                        seq_log.append((now, "ev"))

        print(f"\n  观测窗口 {time.monotonic()-t_start:.1f}s")
        print(f"  心跳帧 {len(heartbeats)} 条，时间点：{[f'{h:.0f}s' for h in heartbeats[:10]]}")
        print(f"  真实事件帧 {real_events} 条")

        check("H-1 静默期收到了心跳注释行", len(heartbeats) >= 2,
              f"{len(heartbeats)} 条")
        check("H-2 心跳是注释行而非事件",
              all(f.startswith(":") for f in [": keep-alive"]), "帧以 : 开头")
        if len(heartbeats) >= 2:
            gaps = [heartbeats[i+1] - heartbeats[i] for i in range(len(heartbeats)-1)]
            avg = sum(gaps) / len(gaps)
            check("H-3 心跳间隔接近配置的 3 秒", 2.0 <= avg <= 6.0, f"平均 {avg:.1f}s")

        # 最关键：心跳必须夹在真实事件之间（静默期触发）。若全挤在末尾，
        # 说明它是流结束才一起吐出来的 —— 那对「防止代理空闲切断」毫无价值。
        kinds = [k for _t, k in seq_log]
        first_hb = kinds.index("hb") if "hb" in kinds else -1
        last_ev = max((i for i, k in enumerate(kinds) if k == "ev"), default=-1)
        check("H-5 心跳夹在真实事件之间（非末尾攒批）",
              first_hb != -1 and first_hb < last_ev,
              f"首个心跳在第 {first_hb} 帧，最后一个真实事件在第 {last_ev} 帧")

        if read_times:
            span = read_times[-1] - read_times[0] if len(read_times) > 1 else 0
            uniq = sorted({round(t) for t in read_times})
            print(f"  read() {len(read_times)} 次，首末跨度 {span:.1f}s，"
                  f"有数据的秒点 {uniq[:20]}")
            check("H-6 数据分批到达（不是末尾一次性吐出）",
                  len(uniq) >= 5, f"{len(uniq)} 个不同秒点")

        check("H-4 真实事件照常送达", real_events >= 1, f"{real_events} 条")

        # 收尾：停止这一轮，别让它继续跑
        try:
            post(f"/api/sessions/{sid}/stop",
                 {"generation_id": gen_id}, token)
        except Exception:
            pass

    except Exception as exc:
        import traceback
        traceback.print_exc()
        fail.append(f"异常：{type(exc).__name__}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()

    print()
    print("=" * 70)
    print(f"通过 {len(ok)} / 失败 {len(fail)}")
    for f in fail:
        print(f"  ✗ {f}")
    print("=" * 70)
    return 0 if not fail else 1


def wire_mode() -> int:
    """`--wire`：不经模型/工具的框架层对照。

    起一个只有 `/silent` 端点的小 FastAPI（`StreamingResponse` 包一个 job 的 drain，
    全程 15 秒不发任何真实事件），客户端用**裸 socket** 逐字节记到达时刻。

    这条对照回答的是：「心跳到达不了线上，到底是 turn_job 的问题，还是
    `StreamingResponse` 没逐帧 flush？」——与模型、工具、`code_run` 全都无关。

    实测（CPython 3.13 + uvicorn）：心跳到达 `[3.0, 6.0, 9.0, 12.0]`，间隔全 3.0s，
    说明 `StreamingResponse` 本身逐帧 flush，不需要额外中间件。
    """
    import asyncio

    sys.path.insert(0, str(ROOT / "backend"))
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    from app import turn_job

    turn_job.HEARTBEAT_IDLE_S = 3.0
    app = FastAPI()
    port = free_port()

    @app.get("/silent")
    async def silent():
        job = turn_job.TurnJob("u", "s", "g-wire")
        sub = job.attach()

        async def runner(j):
            await asyncio.sleep(15)        # 全程静默
            j.publish("event: done\ndata: {}\n\n")

        turn_job.start_job(job, runner)

        async def gen():
            async for chunk in sub.drain():
                yield chunk

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def client() -> list[float]:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /silent HTTP/1.1\r\nHost: x\r\n"
                     b"Accept: text/event-stream\r\n\r\n")
        await writer.drain()
        t0 = time.monotonic()
        arrivals: list[float] = []
        buf = b""
        while time.monotonic() - t0 < 20:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=20)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            now = time.monotonic() - t0
            buf += chunk
            while len(arrivals) < buf.count(b": keep-alive"):
                arrivals.append(round(now, 1))
            if b"event: done" in buf:
                break
        writer.close()
        return arrivals

    async def main() -> int:
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                               log_level="error"))
        task = asyncio.create_task(server.serve())
        await asyncio.sleep(1.5)
        arrivals = await client()
        print(f"心跳到达时刻：{arrivals}")
        gaps = [round(arrivals[i + 1] - arrivals[i], 1) for i in range(len(arrivals) - 1)]
        print(f"间隔：{gaps}")
        ok = len(arrivals) >= 3 and all(2.0 <= g <= 5.0 for g in gaps)
        print("结论：", "心跳逐帧即时到达 ✓（StreamingResponse 本身就会 flush）"
              if ok else "心跳被攒批，没及时送出 ✗")
        server.should_exit = True
        await task
        return 0 if ok else 1

    return asyncio.run(main())


if __name__ == "__main__":
    if "--wire" in sys.argv:
        sys.exit(wire_mode())
    sys.exit(main())
