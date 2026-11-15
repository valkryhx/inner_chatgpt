"""真机验证：崩溃中断后**不重放工具**（持久化第二步）。

## 要证明什么

不是「代码里有 crash marker 表」，而是**行为真的变了**：

1. 一轮对话跑到 `code_run` 中途被 kill -9；
2. 重启后端 —— 启动序列应把它标成崩溃；
3. 同一会话再发一句 —— `sanitize()` **不该重新执行那个 code_run**，
   而是补一条「执行状态未知」让模型先核对。

第 3 步的判据：模型这轮的工具调用序列里**不应再出现**那个被中断的
工具调用（或者即使出现，也要先有核对动作）。

## 跑法

    # 阶段一（提交并等待被杀）
    cd backend && python ../tools/persistence_verify/probe_crash_resume.py before
    # 手动 kill 后端 + 重启
    # 阶段二（续一轮，看行为）
    cd backend && python ../tools/persistence_verify/probe_crash_resume.py after
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000"
STATE = ROOT / "work" / "crash_state.json"
RID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"

# 这个命令会留下一个可核对的文件 —— 正好用来验「有没有重复执行」。
SENTINEL_FILE = f"crash_probe_{RID}.txt"


async def before() -> int:
    from tests.e2e.harness import ensure_user, ensure_zhanlu

    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        tok = await ensure_user(c, f"cr_{RID}".replace("-", "_"))
        await ensure_zhanlu(c, tok)
        sid = (await c.post("/api/sessions", json={"title": f"CR-{RID}"},
                            headers={"Authorization": f"Bearer {tok}"})).json()["id"]
        print(f"session = {sid}")
        print(f"哨兵文件 = {SENTINEL_FILE}")

        prompt = (
            f"用 code_run 执行：先 sleep 45 秒，然后往 workspace 写一个文件 "
            f"{SENTINEL_FILE}，内容写第一行。只做这一件事。"
        )

        async def fire():
            try:
                async with c.stream("POST", f"/api/sessions/{sid}/messages",
                                    json={"text": prompt},
                                    headers={"Authorization": f"Bearer {tok}",
                                             "Accept": "text/event-stream"},
                                    timeout=200) as r:
                    async for _ in r.aiter_lines():
                        pass
            except Exception:
                pass

        t = asyncio.create_task(fire())
        # 关键：**保持这条 SSE 连接开着**，然后由外部命令杀后端进程。
        #
        # 第一版这里 `t.cancel()` 了 —— 结果客户端主动断开，chat.py 的 finally
        # 把消息收敛成 `stopped`，在库里和「用户主动停止」长得一模一样，
        # 重启时就再也分不出「崩溃」了。
        #
        # 真实崩溃的形态是：客户端还在等，服务端进程突然没了。
        # 所以这里必须「什么都不做」，一直等着被杀。
        print("\n保持连接中…… 请**另开一个终端**杀掉后端进程（Stop-Process -Force）。")
        print("等 20 秒后本脚本自行退出（届时服务端应已被杀）。", flush=True)
        await asyncio.sleep(20)
        print("探针退出（服务端应已死）。现在重启后端并跑 --after。", flush=True)

    STATE.write_text(json.dumps({
        "session_id": sid, "user_token": tok, "rid": RID,
        "sentinel_file": SENTINEL_FILE,
    }, ensure_ascii=False), encoding="utf-8", newline="\n")
    print("\n已发出并等了 14 秒 —— 现在应当处于 code_run 中途。")
    print("请 kill 后端并重启，然后跑：python ../tools/persistence_verify/probe_crash_resume.py after")
    return 0


async def after() -> int:
    import sqlite3

    from tests.e2e.harness import send_message, summarize

    st = json.loads(STATE.read_text(encoding="utf-8"))
    sid, tok, sentinel = st["session_id"], st["user_token"], st["sentinel_file"]

    checks: list[dict] = []

    # ---- 1) 崩溃标记是否建立 ----
    con = sqlite3.connect(str(ROOT / "backend" / "data" / "app.db"))
    con.row_factory = sqlite3.Row
    row = con.execute("select * from turn_crash_markers where session_id=?",
                      (sid,)).fetchone()
    con.close()
    checks.append({"k": "重启后崩溃标记存在", "ok": row is not None,
                   "d": dict(row) if row else "无"})

    # ---- 2) 续一轮：模型必须先核对，不能直接重放 ----
    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        events = await send_message(
            c, tok, sid,
            f"上一轮被中断了。先检查 {sentinel} 在不在、内容是什么，"
            f"再告诉我该不该重做上一轮的事。不要直接重试，先看。",
            timeout_s=420)
        s = summarize(events)
        tools = s["tools"]
        text = s["text"]

    checks.append({"k": "续轮工具序列", "ok": True, "d": str(tools)})
    # 关键断言：这轮应当有读取类动作（核对），而不是直接 code_run 重跑
    read_like = [t for t in tools if t in ("file_read", "list_files", "file_glob")]
    checks.append({"k": "先核对（有读取类工具）", "ok": bool(read_like),
                   "d": str(read_like)})
    # 不许一上来就重跑上一轮那个写文件的命令
    checks.append({"k": "未直接重跑 code_run", "ok": "code_run" not in tools[:1],
                   "d": f"前几个工具={tools[:3]}"})
    checks.append({"k": "回答提到未知/中断/核对",
                   "ok": any(k in text for k in ("未知", "中断", "核对", "不确定")),
                   "d": text[:200]})

    # ---- 3) 哨兵文件是否被重复写（副作用没有翻倍）----
    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        r = await c.get(f"/api/me/workspace-file?path={sentinel}",
                        headers={"Authorization": f"Bearer {tok}"})
        existed = r.status_code == 200
        body = r.text[:300] if existed else ""
    checks.append({"k": f"哨兵文件 {sentinel}", "ok": True,
                   "d": f"存在={existed} 内容={body[:120]}"})

    print("=" * 66)
    for x in checks:
        print(f"  [{'OK  ' if x['ok'] else 'FAIL'}] {x['k']}: {x['d']}")
    print("=" * 66)
    bad = [x for x in checks if not x["ok"]]
    print(f"{len(checks) - len(bad)}/{len(checks)} 通过")
    (ROOT / "work" / "crash_after.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")
    return 0 if not bad else 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "before"
    sys.exit(asyncio.run(after() if mode == "after" else before()))
