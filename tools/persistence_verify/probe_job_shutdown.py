"""验证：进程被硬杀（崩溃）后，重启时 turn job 的遗留被正确收敛。

⚠️ 第一版这条探针写错了一个前提：用 `taskkill //F` 硬杀却断言
「走 lifespan 优雅停止」。硬杀根本不走 lifespan（那是崩溃路径），
所以「消息仍是 streaming」是**正确**的中间态，不是缺陷。

正确的两条判据：

| 路径 | 期望 |
|---|---|
| 硬杀（崩溃） | 重启时 `db.init_db` 先建崩溃标记、再收敛成 stopped（第二步的能力） |
| 优雅退出 | `turn_job.shutdown_all` 取消 job，`_event_stream` 的 finally 收敛成 stopped |

优雅退出那条已由单测 `test_shutdown_all_cancels_live_jobs` 覆盖；
本探针验**崩溃路径**，因为它必须跨进程，只有真机能验。

跑法：cd backend && python ../tools/persistence_verify/probe_job_shutdown.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402

# 端口可被环境变量覆盖：探针常在隔离后端（非 8000）上跑。
BASE = os.environ.get("HLZ_PROBE_BASE") or "http://127.0.0.1:8000"
RID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"
# 库路径可被环境变量覆盖：探针要能在隔离库上跑，否则默认指向生产库。
DB = Path(os.environ.get("HLZ_PROBE_DB") or (ROOT / "backend" / "data" / "app.db"))


def snap(sid: str) -> list[tuple[str, str, int]]:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rs = con.execute(
        "SELECT role, status, length(content) n FROM messages "
        "WHERE session_id=? ORDER BY rowid", (sid,)).fetchall()
    con.close()
    return [(r["role"], r["status"], r["n"]) for r in rs]


def marker(sid: str):
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM turn_crash_markers WHERE session_id=?", (sid,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    con.close()
    return dict(row) if row else None


def find_port_pid(port: int) -> str:
    """只查 PID。**必须在生成开始前调用** —— 这个 PowerShell 查询本身要 1-2 秒，
    在生成开始后再调，模型早就把整篇写完了（实测一次 11352 字）。
    """
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-NetTCPConnection -LocalPort {port} -State Listen).OwningProcess"],
        capture_output=True, text=True).stdout.strip()


def kill_pid(pid: str) -> None:
    """硬杀。参数是 `/PID` 不是 `//PID` —— 后者是 Git Bash 的转义写法，
    从 Python `subprocess` 直接调时 taskkill 会静默失败。"""
    r = subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  [WARN] taskkill 失败 rc={r.returncode}: {r.stdout}{r.stderr}")


def start_backend(launcher: str) -> None:
    subprocess.Popen(
        [sys.executable, launcher],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


async def wait_health(timeout: float = 90) -> bool:
    async with httpx.AsyncClient(base_url=BASE, timeout=2) as c:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                r = await c.get("/api/health")
                if r.status_code == 200:
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1)
    return False


async def main() -> int:
    from tests.e2e.harness import ensure_user, ensure_zhanlu

    async with httpx.AsyncClient(base_url=BASE, timeout=None) as c:
        # 先把 PID 取到手（这一步 1-2 秒），否则生成会在查询期间跑完。
        pid = find_port_pid(8000)
        print(f"  backend pid = {pid}（提前取好，避免杀之前浪费时间）")
        if not pid:
            print("  [FAIL] 找不到监听 8000 的进程")
            return 1

        tok = await ensure_user(c, f"sd_{RID}".replace("-", "_"))
        await ensure_zhanlu(c, tok)
        h = {"Authorization": f"Bearer {tok}"}
        sid = (await c.post("/api/sessions", json={"title": f"SD-{RID}"},
                            headers=h)).json()["id"]
        print(f"session = {sid}")

        got, cur = 0, ""
        async with c.stream("POST", f"/api/sessions/{sid}/messages",
                            json={"text": "写一篇 8000 字长文讲操作系统调度。不要用工具。"},
                            headers={**h, "Accept": "text/event-stream"},
                            timeout=600) as r:
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                if line.startswith("event:"):
                    cur = line[6:].strip()
                elif line.startswith("data:") and cur == "text_delta":
                    got += len(line[5:])
                    if got >= 60:
                        break
        # ⚠️ 不能再 sleep：首字之后模型流得很快（实测 6000 字约 3 秒），
        # 睡一下就生成完了，杀掉时已经没有可控的中间态。
        before = snap(sid)
        print(f"  断开瞬间：{before}")

    print("\n  硬杀后端（模拟崩溃，不走 lifespan）……")
    kill_pid(pid)
    print(f"  killed pid = {pid}")
    await asyncio.sleep(3)
    crashed = snap(sid)
    print(f"  进程死时：{crashed}")
    ok_streaming = bool(crashed) and crashed[-1][1] == "streaming"
    print(f"  [{'PASS' if ok_streaming else 'FAIL'}] 崩溃瞬间消息停在 streaming（证据保留）")

    print("\n  重启后端……")
    start_backend("tools/persistence_verify/launch_verify.py")
    up = await wait_health()
    print(f"  后端已起：{up}")
    await asyncio.sleep(2)

    after = snap(sid)
    m = marker(sid)
    last = after[-1] if after else ("?", "?", 0)
    ok_converged = last[1] == "stopped"
    ok_marker = m is not None
    print(f"  重启后：{after}")
    print(f"  崩溃标记：{m}")
    print(f"  [{'PASS' if ok_converged else 'FAIL'}] 已收敛成 stopped（实际 {last[1]}）")
    print(f"  [{'PASS' if ok_marker else 'FAIL'}] 留下了崩溃标记（下一轮不重放工具）")

    # 收尾：清标记以免影响后续测试会话
    con = sqlite3.connect(str(DB))
    con.execute("DELETE FROM turn_crash_markers WHERE session_id=?", (sid,))
    con.commit()
    con.close()

    return 0 if (ok_streaming and ok_converged and ok_marker) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
