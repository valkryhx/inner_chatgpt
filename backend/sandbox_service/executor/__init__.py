"""执行器工厂：按 `HLZ_SANDBOX_EXECUTOR` 选实现。

三点设计：

1. **未知取值直接报错，不静默退回默认**
   静默退回会让「配错了但看起来在工作」，比直接报错危险得多。
   （对比：`HLZ_SANDBOX_MODE` 的非法值退回 `off` 是安全的，因为 off = 不启用；
   而执行器配错退回 bwrap 会让人以为 gVisor 生效了。）

2. **延迟导入**
   `gvisor.py` 需要 docker SDK，`off` 模式的部署不该被它牵连。

3. **默认 bwrap**
   方案选 A 的落地形态。
"""
from __future__ import annotations

import os
from typing import Callable

from .base import ExecResult, Executor

__all__ = ["Executor", "ExecResult", "get_executor", "VALID_EXECUTORS"]

VALID_EXECUTORS = ("bwrap", "inproc", "gvisor")


def get_executor(kind: str | None = None, **kwargs) -> Executor:
    """构造执行器。

    Args:
        kind: `bwrap` | `inproc` | `gvisor`。None 时读
              `HLZ_SANDBOX_EXECUTOR`，再退回 `bwrap`。
        **kwargs: 透传给具体实现的构造函数。

    Raises:
        ValueError: kind 不在 VALID_EXECUTORS 里。
    """
    if kind is None:
        kind = (os.environ.get("HLZ_SANDBOX_EXECUTOR") or "bwrap").strip().lower()

    # 不给 resolver 时用默认的（从 appdata 卷里的 workspaces/<uid>）。
    # 显式传入优先，便于测试与多 root 场景。
    if "workspace_resolver" not in kwargs and kind in ("bwrap", "inproc"):
        from ..workspace import make_resolver

        kwargs["workspace_resolver"] = make_resolver()

    if kind == "bwrap":
        from .bwrap import BwrapExecutor

        return BwrapExecutor(**kwargs)
    if kind == "inproc":
        from .inproc import InprocExecutor

        return InprocExecutor(**kwargs)
    if kind == "gvisor":
        # 延迟导入：没装 docker SDK / 没 runsc 的部署不该在这里就崩
        from .gvisor import GVisorExecutor

        return GVisorExecutor(**kwargs)

    raise ValueError(
        f"unknown executor: {kind!r}；合法取值 {VALID_EXECUTORS}。"
        f"（不会静默退回默认——那会让人以为配置生效了）"
    )