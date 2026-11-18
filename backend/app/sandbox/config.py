"""沙箱配置：环境变量 + 默认值。

全部环境变量都以 ``HLZ_SANDBOX_`` 开头，与项目既有 ``HLZ_`` 前缀一致。
默认值取自实测（设计文档 §4.1）：镜像 ``python:3.12-slim``、exec 约 250ms、
空闲内存 1.19 MiB。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# 已定镜像（设计文档 §4.1 实测：177MB，Python 3.12.16，3 个包）
# 用 tag 而非 digest：本机镜像站对固定 digest 的拉取不稳定，
# 且 MVP 只在本机跑。换机器前请按设计文档 §9.1-R8 固定 digest。
DEFAULT_IMAGE = "python:3.12-slim"

# MVP 白名单：hx / xh（设计文档 §9.3 Q2）
DEFAULT_MVP_USERS = "u_c56b8337dd09,u_343a46896bff"

VALID_MODES = ("off", "auto", "strict")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class SandboxConfig:
    """一次读取、不可变。改配置需重启（与项目其它配置一致）。"""

    mode: str = "off"
    image: str = DEFAULT_IMAGE
    idle_ttl_s: int = 900
    max_containers: int = 4
    create_timeout_s: float = 30.0
    exec_timeout_s: float = 120.0
    memory: str = "1g"
    cpus: str = "1"
    pids_limit: int = 256
    tmpfs_size: str = "256m"
    mvp_users: tuple[str, ...] = field(default_factory=tuple)

    # ---------- 派生 ----------

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def user_in_scope(self, user_id: str) -> bool:
        """MVP 白名单：空列表 = 全部用户（不推荐，见设计文档 §5.4）。"""
        if not self.mvp_users:
            return True
        return user_id in self.mvp_users


def load_config() -> SandboxConfig:
    """从环境变量读一次。非法 mode 一律退回 ``off``（安全默认）。"""
    mode = _env("HLZ_SANDBOX_MODE", "off").lower()
    if mode not in VALID_MODES:
        mode = "off"

    # 注意：这里**不能**用 _env() 的 "or default" 语义 ——
    # 它会把显式空串 ``HLZ_SANDBOX_MVP_USERS=`` 当成"未设置"，
    # 于是「空 = 全部用户」这条文档语义根本表达不出来（本处曾被测试抓出）。
    # 改为：变量存在就以它的值为准（空串 -> 空元组 -> 全部），不存在才用默认白名单。
    raw_users = (
        os.environ["HLZ_SANDBOX_MVP_USERS"]
        if "HLZ_SANDBOX_MVP_USERS" in os.environ
        else DEFAULT_MVP_USERS
    )
    users = tuple(u.strip() for u in raw_users.split(",") if u.strip())

    return SandboxConfig(
        mode=mode,
        image=_env("HLZ_SANDBOX_IMAGE", DEFAULT_IMAGE),
        idle_ttl_s=_env_int("HLZ_SANDBOX_IDLE_TTL_S", 900),
        max_containers=_env_int("HLZ_SANDBOX_MAX_CONTAINERS", 4),
        create_timeout_s=_env_float("HLZ_SANDBOX_CREATE_TIMEOUT_S", 30.0),
        exec_timeout_s=_env_float("HLZ_SANDBOX_EXEC_TIMEOUT_S", 120.0),
        memory=_env("HLZ_SANDBOX_MEMORY", "1g"),
        cpus=_env("HLZ_SANDBOX_CPUS", "1"),
        pids_limit=_env_int("HLZ_SANDBOX_PIDS_LIMIT", 256),
        tmpfs_size=_env("HLZ_SANDBOX_TMPFS_SIZE", "256m"),
        mvp_users=users,
    )


_cached: SandboxConfig | None = None


def get_config(*, reload: bool = False) -> SandboxConfig:
    """进程内单例。``reload=True`` 供测试使用。"""
    global _cached
    if _cached is None or reload:
        _cached = load_config()
    return _cached