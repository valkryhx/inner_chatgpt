"""唯一分流判定点（设计文档 §5.2）。

**所有装配点只问这里，不各自判断。**
教训来自记忆系统 M5 的越权缺陷：判据一旦散开，必然漏一处。

本模块**不依赖 docker、不做 IO** —— 纯函数，可以在任何上下文（含单测）安全调用。
"""
from __future__ import annotations

from .config import SandboxConfig, get_config

# 走容器的工具（设计文档 §5.1）。
#
# 为什么只有这三个：
# - code_run 执行任意用户代码，是第一优先级；
# - file_write / file_patch 必须跟着进，否则写操作能从容器旁边绕过去
#   （本项目 run_guard 的审计一行 subprocess 就能绕，不像 CC 有权限系统兜底）。
# 读类工具留在进程内：容器里没有 Skill 目录与仓库挂载点，
# 全挂进去等于挂半个宿主，隔离就白做了。
SANDBOXED_TOOLS = frozenset({"code_run", "file_write", "file_patch"})


def should_use_sandbox(tool_name: str, user_id: str, cfg: SandboxConfig | None = None) -> bool:
    """本次调用是否走容器。三档语义见 ``config.SandboxConfig.mode``。

    注意：本函数**只回答"要不要走"**，不回答"走不走得通"。
    容器不可用时的降级/失败由 ``manager`` 按 mode 决定（铁律 2：绝不静默降级）。
    """
    cfg = cfg or get_config()
    if not cfg.enabled:
        return False
    if tool_name not in SANDBOXED_TOOLS:
        return False
    return cfg.user_in_scope(user_id)


def sandbox_enabled(cfg: SandboxConfig | None = None) -> bool:
    """总开关。供 UI / 日志问「沙箱是否启用」。"""
    return (cfg or get_config()).enabled


def mode(cfg: SandboxConfig | None = None) -> str:
    return (cfg or get_config()).mode