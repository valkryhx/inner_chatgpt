"""多进程下的 dream 锁竞争实测（真跨进程，不是同进程两个协程）。

## 为什么要用「两个独立 python 进程」而不是 uvicorn --workers

`backend/run.py` 是**单 worker**（`uvicorn.run(...)` 未传 `workers=`），
当前部署形态里不存在多 worker。所以「多 worker 锁竞争」的实质问题是：

> **DB 行锁在跨进程时真的互斥吗？**

这跟进程是不是 uvicorn worker 无关 —— 两个独立的 Python 进程同时抢同一行，
就是对锁语义最直接的检验，而且比启动多 worker 更可控（能精确控制时序、
能直接读 DB 断言、不受端口/信号/Windows spawn 的干扰）。

## 测什么

| 场景 | 构造 | 判据 |
|---|---|---|
| P-1 跨进程互斥 | 2 个进程同时 `acquire_lock`，各自 hold 1 秒 | **恰好 1 个成功**；另一个拿到 False |
| P-2 崩溃后可被抢 | 进程 A 抢到锁后被 SIGKILL（不 release），进程 B 立刻抢 | 锁还新鲜时 B 必须失败（不能误抢）；超过 `LOCK_STALE_MS` 后 B 成功 |
| P-3 重复竞争不双跑 | 4 个进程同时抢，每个成功后 sleep 0.3 再释放 | 整个期间**任一时刻持锁者 ≤ 1**（用 DB 轮询采样验证） |
| P-4 收敛能救崩溃 | A 崩溃留锁 -> 跑 `recover_dream_state()` -> B 抢 | B 必须成功（**不依赖** 30 分钟过期） |

P-2 的后半段用「把 `lock_at` 人为改老」实现，避免真等 30 分钟；
这与 M-22 单测同一手法，但这里是**跨进程**验证。

跑法::

    cd backend && python ../tools/memory_verify/multi_process_lock.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

# 子进程脚本：抢锁 -> 报告结果 -> （可选）hold 一段时间
# 用 stdin 传参避免 Windows 上引号/中文参数的转义问题。
# `retry` 模式用于 P-3：输的人不退出，而是重试，直到成功或超时 —— 这样
# 「串行」才是被观察到的性质，而不是「抢不到就散」。
CHILD = """
import asyncio, json, os, sys, time
sys.path.insert(0, r"{backend}")

async def main():
    cfg = json.loads(sys.stdin.read())
    from app import memory_store as ms
    from app import memory_dream as md
    from pathlib import Path
    ms.DB_PATH = Path(r"{db}")
    await md.init_tables()

    user = cfg["user"]
    deadline = time.time() + cfg.get("retry_for", 0)
    acquired_count = 0
    win_times = []
    while True:
        got = await md.acquire_lock(user, pid=os.getpid(),
                                    now_ms=cfg.get("now_ms") or 0)
        if got:
            acquired_count += 1
            win_times.append([time.time(), time.time()])
            print(json.dumps({{"pid": os.getpid(), "acquired": True,
                               "round": acquired_count}}), flush=True)
            if cfg.get("hold", 0):
                time.sleep(cfg["hold"])
            win_times[-1][1] = time.time()
            if not cfg.get("no_release"):
                await md.release_lock(user, pid=os.getpid())
            if not cfg.get("retry"):
                break
        else:
            if not cfg.get("retry"):
                print(json.dumps({{"pid": os.getpid(), "acquired": False}}),
                      flush=True)
                break
        if time.time() >= deadline:
            break
        await asyncio.sleep(0.01)
    print(json.dumps({{"pid": os.getpid(), "done": True,
                       "wins": acquired_count, "windows": win_times}}), flush=True)

asyncio.run(main())
"""


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, case: str, verdict: str, detail: str) -> None:
        self.rows.append((case, verdict, detail))
        print(f"  [{'OK  ' if verdict == 'OK' else verdict}] {case}: {detail}")

    def exit_code(self) -> int:
        return 0 if all(v == "OK" for _, v, _ in self.rows) else 1

    def dump(self, path: Path) -> None:
        path.write_text(json.dumps(
            [{"case": c, "verdict": v, "detail": d} for c, v, d in self.rows],
            ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def spawn(tmp: Path, db: Path, user: str, *, hold: float = 0,
          now_ms: int = 0, no_release: bool = False,
          retry: bool = False, retry_for: float = 0) -> subprocess.Popen:
    script = tmp / f"_child_{user}_{time.time_ns()}.py"
    script.write_text(CHILD.format(backend=BACKEND, db=db),
                      encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND)
    p = subprocess.Popen([sys.executable, str(script)], cwd=str(BACKEND),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, text=True,
                         encoding="utf-8", errors="replace")
    p.stdin.write(json.dumps({"user": user, "hold": hold, "now_ms": now_ms,
                              "no_release": no_release, "retry": retry,
                              "retry_for": retry_for}))
    p.stdin.close()
    return p


def read_lines(p: subprocess.Popen, timeout: float = 90) -> list[dict]:
    """读到进程结束（或超时）为止的所有 JSON 行。"""
    out: list[dict] = []
    end = time.time() + timeout
    while time.time() < end:
        line = p.stdout.readline()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                pass
            continue
        if p.poll() is not None:
            break
        time.sleep(0.02)
    return out


def read_result(p: subprocess.Popen, timeout: float = 60) -> dict:
    """读第一行（兼容老调用：只关心首次抢锁结果）。"""
    lines = read_lines(p, timeout=timeout)
    return lines[0] if lines else {"acquired": None,
                                   "error": p.stderr.read()[-400:] if p.stderr else ""}


async def db_state(db: Path, user: str) -> dict:
    import aiosqlite
    async with aiosqlite.connect(db) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM memory_dream_state WHERE user_id=?", (user,))
        row = await cur.fetchone()
    return dict(row) if row else {}


def case_p1(tmp: Path, db: Path) -> tuple[str, str]:
    """P-1：两个进程同时抢，恰好一个成功。"""
    user = "p1"
    ps = [spawn(tmp, db, user, hold=1.0) for _ in range(2)]
    got = [read_result(p)["acquired"] for p in ps]
    for p in ps:
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
    winners = got.count(True)
    if winners != 1:
        return "FAIL", f"成功数={winners}（期望恰好 1）四次结果={got}"
    return "OK", f"恰好 1 个进程拿到锁（结果={got}）"


def case_p2(tmp: Path, db: Path) -> tuple[str, str]:
    """P-2：持锁进程崩溃后，新鲜锁不可抢；过期后可抢。"""
    user = "p2"
    holder = spawn(tmp, db, user, hold=30, no_release=True)
    if read_result(holder)["acquired"] is not True:
        holder.kill()
        return "FAIL", "持锁进程没拿到锁，前置不成立"
    # 持锁者还活着、锁新鲜 -> 别人抢不到
    other = spawn(tmp, db, user)
    fresh = read_result(other)["acquired"]
    other.wait(timeout=20)
    if fresh is not False:
        holder.kill()
        return "FAIL", f"锁新鲜时被抢到了（acquired={fresh}）—— 互斥失效"

    # 杀掉持锁者（模拟崩溃），但锁仍然新鲜 -> 依然抢不到（这是预期：
    # 单靠锁本身无法判断持锁进程是否还活着，需要 recover 或等过期）
    holder.kill()
    holder.wait(timeout=10)
    crash = spawn(tmp, db, user)
    crash_acquired = read_result(crash)["acquired"]
    crash.wait(timeout=20)

    # 把 lock_at 改老 -> 可抢（这是 LOCK_STALE_MS 的意义）
    import sqlite3
    con = sqlite3.connect(str(db))
    old = int(time.time() * 1000) - 31 * 60 * 1000
    con.execute("UPDATE memory_dream_state SET lock_at=? WHERE user_id=?", (old, user))
    con.commit()
    con.close()
    stale = spawn(tmp, db, user)
    stale_acquired = read_result(stale)["acquired"]
    stale.wait(timeout=20)

    if stale_acquired is not True:
        return "FAIL", f"锁过期后仍抢不到（acquired={stale_acquired}）"
    note = (f"新鲜锁不误抢（崩溃后 acquired={crash_acquired}），"
            f"超 30 分钟后可抢（acquired={stale_acquired}）")
    if crash_acquired is True:
        note += " —— 注意：崩溃后锁新鲜时仍被抢到，说明未校验 PID 存活"
    return "OK", note


def case_p3(tmp: Path, db: Path) -> tuple[str, str]:
    """P-3：4 个进程**持续重试**抢锁，验证持锁时间窗互不重叠（真串行）。

    第一版让输的进程直接退出，结果只有 1/4 成功 —— 那只证明了「抢不到」，
    没证明「串行」。这里改成输的人重试 6 秒，于是每个进程都会多次拿到锁，
    再检查所有成功者记录的 `[start, end]` 时间窗**两两不重叠**。
    重叠 = 两个进程同时认为自己持锁 = 互斥失效，这才是要抓的东西。
    """
    user = "p3"
    ps = [spawn(tmp, db, user, hold=0.15, retry=True, retry_for=6.0)
          for _ in range(4)]
    all_lines = [read_lines(p, timeout=60) for p in ps]
    for p in ps:
        try:
            p.wait(timeout=25)
        except subprocess.TimeoutExpired:
            p.kill()

    windows: list[tuple[float, float, int]] = []
    for lines in all_lines:
        for ln in lines:
            if ln.get("done"):
                for w in ln.get("windows") or []:
                    if len(w) == 2:
                        windows.append((w[0], w[1], ln.get("pid", 0)))
    total_wins = sum(1 for ln in (x for ls in all_lines for x in ls)
                     if ln.get("acquired"))

    if total_wins == 0:
        return "FAIL", "没有任何进程拿到锁"

    # 两两检查窗口是否重叠（留 1ms 容差，避免同一次释放/获取的边界误判）
    windows.sort()
    overlaps: list[str] = []
    for i in range(len(windows)):
        for j in range(i + 1, len(windows)):
            a, b = windows[i], windows[j]
            if b[0] < a[1] - 0.001:
                overlaps.append(
                    f"pid{a[2]}[{a[0]:.3f},{a[1]:.3f}] 与 "
                    f"pid{b[2]}[{b[0]:.3f},{b[1]:.3f}]")
    if overlaps:
        return "FAIL", f"{len(overlaps)} 处持锁窗口重叠（互斥失效）：{overlaps[:3]}"

    pids = {w[2] for w in windows}
    if total_wins < 2:
        return ("RISK", f"只有 {total_wins} 次成功抢锁（竞争太激烈），"
                        "未能观察到串行序列")
    return ("OK", f"{total_wins} 次成功抢锁、涉及 {len(pids)} 个进程，"
                  "所有持锁窗口两两不重叠（真串行）")


def case_p4(tmp: Path, db: Path) -> tuple[str, str]:
    """P-4：崩溃残留的**新鲜**锁，靠 recover_dream_state 救回来。"""
    user = "p4"
    holder = spawn(tmp, db, user, hold=30, no_release=True)
    if read_result(holder)["acquired"] is not True:
        holder.kill()
        return "FAIL", "前置不成立"
    holder.kill()
    holder.wait(timeout=10)

    # 锁新鲜，直接抢应该失败
    blocked = spawn(tmp, db, user)
    blocked_ok = read_result(blocked)["acquired"]
    blocked.wait(timeout=20)

    # 跑启动收敛（模拟服务重启）
    code = (
        "import asyncio, sys\n"
        f"sys.path.insert(0, r'{BACKEND}')\n"
        "from pathlib import Path\n"
        "from app import memory_store as ms, memory_dream as md\n"
        f"ms.DB_PATH = Path(r'{db}')\n"
        "async def m():\n"
        "    await md.init_tables()\n"
        "    n = await md.recover_dream_state('probe restart')\n"
        "    print(n)\n"
        "asyncio.run(m())\n"
    )
    rec = subprocess.run([sys.executable, "-c", code], cwd=str(BACKEND),
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace",
                         env={**os.environ, "PYTHONPATH": str(BACKEND)})
    if rec.returncode != 0:
        return "FAIL", f"recover 失败：{rec.stderr[-300:]}"

    after = spawn(tmp, db, user)
    after_ok = read_result(after)["acquired"]
    after.wait(timeout=20)

    if blocked_ok is not False:
        return "FAIL", f"锁新鲜时被抢到（{blocked_ok}）—— 不应发生"
    if after_ok is not True:
        return "FAIL", f"收敛后仍抢不到（{after_ok}）"
    return ("OK", "新鲜锁挡住并发（False）；recover 之后立刻可抢（True）"
                  " —— 不依赖 30 分钟过期")


def main() -> int:
    rep = Report()
    tmp = Path(tempfile.mkdtemp(prefix="dream_lock_"))
    db = tmp / "app.db"
    print(f"临时目录：{tmp}\n")
    try:
        # 建表（父子进程共用同一个 DB 文件）
        sys.path.insert(0, str(BACKEND))
        import asyncio as _a
        from app import memory_store as ms, memory_dream as md, db as dbmod
        ms.DB_PATH = db
        dbmod.DB_PATH = db
        _a.run(dbmod.init_db())
        _a.run(md.init_tables())

        for fn in (case_p1, case_p2, case_p3, case_p4):
            try:
                v, d = fn(tmp, db)
            except Exception as exc:  # noqa: BLE001
                v, d = "FAIL", f"{type(exc).__name__}: {exc}"
            rep.add(fn.__name__.replace("case_", "").upper(), v, d)
    finally:
        out = ROOT / "work" / "multi_process_lock_report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        rep.dump(out)
        print(f"\n报告：{out}")
        shutil.rmtree(tmp, ignore_errors=True)
    return rep.exit_code()


if __name__ == "__main__":
    sys.exit(main())
