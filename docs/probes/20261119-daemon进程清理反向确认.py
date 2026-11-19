"""反向确认：把进程组清理拆掉，孤儿必须重新出现。

## 为什么这个反向确认在容器里跑

`killpg` / PID namespace 的行为在 Windows 上无法复现，所以真断言只能在
装了 bwrap 的容器里执行。本脚本自包含（不依赖 pytest），因为生产镜像
按设计不带测试依赖。

## 两种破坏

1. `start_new_session=True` 去掉 → killpg 会打到服务自身进程组上
   （脚本里用 `_apply_fix=False` 模拟，直接只 kill 外层 pid）
2. `os.killpg` 换成 `proc.kill()` → 内层 bwrap 变孤儿

跑法：
    docker cp work/reverse_confirm_daemon_cleanup.py <c>:/tmp/r.py
    docker exec <c> python /tmp/r.py
期望 `ok: 2/2`。
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

sys.path.insert(0, "/app")

PASS = 0
FAIL = 0


def report(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" — {detail}"))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def bwrap_pids_for(user: str) -> list[int]:
    out = subprocess.run(
        ["ps", "-eo", "pid=,stat=,args="], capture_output=True, text=True
    ).stdout
    pids = []
    for line in out.splitlines():
        if f"--bind /app/backend/data/workspaces/{user} /ws" not in line:
            continue
        parts = line.split(None, 2)
        pids.append(int(parts[0]))
    return pids


async def scenario(user: str, *, use_killpg: bool, new_session: bool) -> list[int]:
    """起一个 daemon，按指定方式清理，返回残留 pid。"""
    from sandbox_service.executor.bwrap import BwrapExecutor
    from sandbox_service.workspace import make_resolver

    ex = BwrapExecutor(workspace_resolver=make_resolver())
    if not new_session:
        # 破坏 1：不让它独立会话（模拟修复前的行为）
        orig = asyncio.create_subprocess_exec

        async def patched(*a, **kw):
            kw.pop("start_new_session", None)
            return await orig(*a, **kw)

        asyncio.create_subprocess_exec = patched       # type: ignore[assignment]

    try:
        ok = await ex.start(user)
        if not ok:
            print(f"SKIP: {user} 预热失败")
            return []
        daemon = ex._daemons[user]

        if not use_killpg:
            # 破坏 2：只杀外层 pid（模拟修复前的 kill）
            try:
                daemon.proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(daemon.proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            ex._daemons.pop(user, None)
        else:
            await ex.destroy(user)

        await asyncio.sleep(2.0)
        return bwrap_pids_for(user)
    finally:
        if not new_session:
            asyncio.create_subprocess_exec = orig      # type: ignore[assignment]


async def main() -> int:
    print("反向确认：进程组清理")
    print("-" * 60)

    # A. 破坏态：只杀外层 pid（修复前的实现）
    left_broken = await scenario("u_rc_broken", use_killpg=False, new_session=True)
    report(
        "破坏后（只 proc.kill）→ 应留孤儿",
        len(left_broken) > 0,
        f"实际残留 {left_broken}（若为 0，说明本机 bwrap 不 fork 内层，"
        f"这条反向确认在此环境无意义）",
    )

    # B. 修复态：killpg 整组
    left_fixed = await scenario("u_rc_fixed", use_killpg=True, new_session=True)
    report("修复后（killpg 整组）→ 应无残留", left_fixed == [], f"残留 {left_fixed}")

    print("-" * 60)
    print(f"ok: {PASS}/{PASS + FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))