"""沙箱子系统（MVP）。

设计文档：``docs/20261118-沙箱MVP设计与实施.md``
前序方案：``docs/20261111-code_run容器隔离方案.md``（架构来源）

模块职责：
- ``config``  环境变量与默认值
- ``policy``  唯一的工具分流判定点
- ``probe``   启动自检与残留清理
- ``runtime`` 单容器生命周期
- ``pool``    每用户注册表 + 懒创建 + TTL + LRU
- ``manager`` 对外唯一门面
"""
from __future__ import annotations

from .config import SandboxConfig, get_config
from .errors import SandboxError, SandboxExecError, SandboxTimeout, SandboxUnavailable

__all__ = [
    "SandboxConfig",
    "get_config",
    "SandboxError",
    "SandboxUnavailable",
    "SandboxExecError",
    "SandboxTimeout",
]