"""能力边界：闭世界授权（参考 GenericAgent capabilities.py）。

核心原则：prompt 里写「不要用某工具」不是安全边界，**工具 schema 里没有它才是**。
子代理的 ToolRegistry 里根本不存在编排工具 → 模型连尝试调用的机会都没有。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..tools import Tool, ToolRegistry

# 编排面：只有 root / lead 能用
ORCHESTRATION_TOOLS = frozenset({
    "spawn_agent",
    "list_agents",
    "wait_agent",
    "read_agent_result",
    "close_agent",
    # 预留给 team 阶段（现在不存在也无害）
    "send_message",
    "task_create",
    "task_get",
    "task_update",
    "task_list",
    "team_status",
})

# 只读角色可用的工具（explore / plan / review）
READ_ONLY_TOOLS = frozenset({
    "file_read",
    "list_files",
    "load_skill",
})

ROLE_CHOICES = ("general", "explore", "plan", "review")
READ_ONLY_ROLES = frozenset({"explore", "plan", "review"})


@dataclass(frozen=True)
class CapabilityProfile:
    role: str
    allowed: frozenset[str]
    allow_delegation: bool = False

    def allows(self, tool_name: str) -> bool:
        return tool_name in self.allowed

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "allowed_tools": sorted(self.allowed),
            "allow_delegation": self.allow_delegation,
        }


def build_capability_profile(*, role: str, available_tools: list[str],
                             allow_delegation: bool = False) -> CapabilityProfile:
    """构建子代理的闭世界工具集。

    - role 不在枚举内 → 视为 general（宽容，但会在工具层被规范化）
    - general：全部非编排工具
    - explore/plan/review：只有 READ_ONLY_TOOLS 与已存在工具的交集
    - 除非 allow_delegation=True，编排工具一律剥离
    """
    available = {str(t) for t in (available_tools or [])}
    role = (role or "general").strip().lower()
    if role not in ROLE_CHOICES:
        role = "general"

    if role in READ_ONLY_ROLES:
        allowed = set(available & READ_ONLY_TOOLS)
    else:
        allowed = set(available) - ORCHESTRATION_TOOLS

    if not allow_delegation:
        allowed -= ORCHESTRATION_TOOLS
    else:
        allowed |= (available & ORCHESTRATION_TOOLS)

    return CapabilityProfile(
        role=role,
        allowed=frozenset(allowed),
        allow_delegation=bool(allow_delegation),
    )


def build_child_registry(parent: ToolRegistry, profile: CapabilityProfile) -> ToolRegistry:
    """按 profile 从父 registry 裁剪出一个新 registry（闭世界）。"""
    child = ToolRegistry()
    for tool in parent.tools:
        if profile.allows(tool.name):
            child.add(tool)
    return child
