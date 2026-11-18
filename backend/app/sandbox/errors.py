"""沙箱异常类型。

设计原则（设计文档 §2 铁律 2）：**绝不静默降级**。
每种失败都有独立类型，调用方必须显式决定「失败」还是「降级并留痕」，
不允许 catch 一个宽泛的 Exception 就当没发生。
"""
from __future__ import annotations


class SandboxError(Exception):
    """沙箱相关错误的基类。"""


class SandboxUnavailable(SandboxError):
    """沙箱不可用（Docker 未运行 / 镜像缺失 / 配置非法）。

    ``reason`` 是给用户看的短句，会出现在工具结果里（降级时必须留痕）。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SandboxExecError(SandboxError):
    """在容器内执行失败（非零退出码之外的问题：exec 本身失败、容器消失等）。"""


class SandboxTimeout(SandboxError):
    """单次执行超时。已尽力终止容器内进程。"""

    def __init__(self, timeout_s: float) -> None:
        super().__init__(f"执行超时（{timeout_s}s），已终止")
        self.timeout_s = timeout_s