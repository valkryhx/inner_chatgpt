"""`inproc` 后端：现有 Docker 实现的**回滚路径**（方案 §6.3）。

## 为什么保留

方案 §6.3 的硬要求：服务化上线后出问题，要能**一个环境变量切回来**，
不用回滚镜像。所以 `runtime.py` / `pool.py` 的 Docker 实现不删。

## 它做什么

直接委托给现有的 `manager`——一行逻辑都不复制。复制会让两条路径慢慢分叉，
到时候「回滚了但行为不一样」就失去回滚的意义。
"""
from __future__ import annotations

from typing import Any


class InprocBackend:
    """委托给现有 `SandboxManager`（Docker 实现）。"""

    name = "inproc"

    def __init__(self) -> None:
        self._manager: Any = None

    def _get_manager(self):
        """延迟取 manager：避免 import 时就触发 pool/probe 初始化。"""
        if self._manager is None:
            from ..manager import get_manager

            self._manager = get_manager()
        return self._manager

    async def health(self) -> tuple[bool, str]:
        """问 Docker 能不能用（这条路径确实用 Docker，所以探针对）。

        延迟取 manager，避免 import 时就触发 pool/probe 初始化。
        """
        from .. import probe

        cfg = self._get_manager().cfg
        return probe.check(cfg)

    async def exec_code(
        self,
        user_id: str,
        code: str,
        *,
        code_type: str = "python",
        timeout: float = 60.0,
        root: str = "",
        session_id: str = "",
        generation_id: str = "",
        agent_id: str = "root",
    ) -> tuple[bool, str, str]:
        return await self._get_manager().exec_code(
            user_id,
            code,
            code_type=code_type,
            timeout=timeout,
            root=root,
            session_id=session_id,
            generation_id=generation_id,
            agent_id=agent_id,
        )

    async def write_file(self, *a, **kw) -> tuple[bool, str]:
        return await self._get_manager().write_file(*a, **kw)

    async def patch_file(self, *a, **kw) -> tuple[bool, str]:
        return await self._get_manager().patch_file(*a, **kw)