"""进程内执行器：`HLZ_SANDBOX_MODE=off` 与回滚路径用。

## 它做什么

直接 `subprocess` 跑代码，**没有任何隔离**——这正是 `off` 的语义：
与现在（沙箱关闭时走进程内 subprocess）逐字节一致。

## 为什么还需要它

1. `off` 模式要有实现（方案 §8.2.2）
2. **回滚路径**：服务化出问题时一个环境变量切回来，不动镜像（方案 §6.3）

## 它不做什么

不做路径守卫、不做资源限制——那些在 `app/run_guard.py` 和调用方。
这里只负责「把代码跑起来并返回结果」，保持与 bwrap 实现同构。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from .base import ExecResult


class InprocExecutor:
    """直接在当前进程的子进程里执行。**无隔离**。"""

    _started: set[str]

    def __init__(
        self,
        *,
        workspace_resolver: Optional[Callable[[str], Optional[Path]]] = None,
    ) -> None:
        self._resolve = workspace_resolver or (lambda uid: None)
        self._started = set()

    async def start(self, user_id: str) -> None:
        self._started.add(user_id)

    async def destroy(self, user_id: str) -> None:
        self._started.discard(user_id)

    async def health(self) -> tuple[bool, str]:
        return True, "ok（进程内，无隔离）"

    async def exec(
        self,
        user_id: str,
        code: str,
        *,
        code_type: str = "python",
        timeout_s: float = 120.0,
        cwd: str = "",
    ) -> ExecResult:
        workspace = self._resolve(user_id)
        workdir = str(workspace) if workspace else os.getcwd()

        argv = (
            ["sh", "-c", code] if code_type == "sh" else [sys.executable, "-c", code]
        )

        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        timed_out = False
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            out_b, err_b = await proc.communicate()

        duration_ms = int((time.monotonic() - started) * 1000)
        stderr = (err_b or b"").decode("utf-8", errors="replace")
        if timed_out:
            stderr += f"\n[sandbox] execution exceeded {timeout_s}s and was killed"

        return ExecResult(
            exit_code=124 if timed_out else (proc.returncode or 0),
            stdout=(out_b or b"").decode("utf-8", errors="replace"),
            stderr=stderr,
            duration_ms=duration_ms,
            truncated=False,
        )