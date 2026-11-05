"""能力边界：闭世界授权。

核心原则：prompt 里写「不要用某工具」不是安全边界，**工具 schema 里没有它才是**。

子代理的统一能力模型（2026-11-05 简化）：
- 子代理 = 同一用户信任域内的完整工作代理；
- 可以使用父代理拥有的全部业务工具：内置文件/代码工具、Skill、MCP；
- **唯一硬边界**：编排工具（spawn/wait/read/list/close，以及未来 team/task 工具）
  永远不进入子代理 registry，因此不会递归派生子代理。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..tools import Tool, ToolRegistry

# 编排面：只有 root / lead 能用。子代理的 registry 里永远不出现这些名字。
ORCHESTRATION_TOOLS = frozenset({
    "spawn_agent",
    "list_agents",
    "wait_agent",
    "read_agent_result",
    "close_agent",
    # 预留给 team 阶段
    "send_message",
    "task_create",
    "task_get",
    "task_update",
    "task_list",
    "team_status",
})


@dataclass(frozen=True)
class CapabilityProfile:
    """子代理的能力快照：只记录允许的工具名。"""

    allowed: frozenset[str]

    def allows(self, tool_name: str) -> bool:
        return tool_name in self.allowed

    def to_dict(self) -> dict:
        return {"allowed_tools": sorted(self.allowed)}


def build_capability_profile(*, available_tools: list[str]) -> CapabilityProfile:
    """子代理能力 = 父代理业务工具全集 - 编排工具。

    不过滤 code_run / file_write / MCP / load_skill：
    它们和 root agent 一样受用户身份与 workspace 边界约束。
    """
    available = {str(t) for t in (available_tools or [])}
    return CapabilityProfile(allowed=frozenset(available - ORCHESTRATION_TOOLS))


def build_child_registry(parent: ToolRegistry, profile: CapabilityProfile) -> ToolRegistry:
    """按 profile 从父 registry 裁剪出一个新 registry（闭世界）。"""
    child = ToolRegistry()
    for tool in parent.tools:
        if profile.allows(tool.name):
            child.add(tool)
    return child
