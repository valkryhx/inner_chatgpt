"""调用方能力档位：建 token 时一次性选定，运行期靠 registry 过滤兜底。

设计取向（与产品决定一致）：
- Hlaudz 默认就是 full access，不做逐次人工审批；
- 外部调用者（A2A）能拿到多少能力，由它手里的 token 决定；
- 想降级就在建 token 时选一个更低档位，运行期不再询问任何人。

原则沿用 ``subagent/capabilities.py``：prompt 里写「不要用某工具」不是安全边界，
**registry 里没有它才是**。
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

POLICY_FULL = "full"
POLICY_READ_ONLY = "read_only"
POLICY_CHAT_ONLY = "chat_only"

POLICIES: tuple[str, ...] = (POLICY_FULL, POLICY_READ_ONLY, POLICY_CHAT_ONLY)
DEFAULT_POLICY = POLICY_FULL

# read_only 只保留纯读取类内置工具。
# MCP 工具的能力无法在本进程判定（可能是任意写操作），因此整组不进。
READ_ONLY_TOOLS = frozenset({"file_read", "list_files", "load_skill"})

_ALIASES = {
    "": POLICY_FULL,
    "full": POLICY_FULL,
    "read_only": POLICY_READ_ONLY,
    "readonly": POLICY_READ_ONLY,
    "chat_only": POLICY_CHAT_ONLY,
    "chatonly": POLICY_CHAT_ONLY,
    "none": POLICY_CHAT_ONLY,
}


def normalize(value: Any) -> str:
    """把外部输入收敛为三档之一；None/空串按默认 full。未知值直接报错。"""
    if value is None:
        return DEFAULT_POLICY
    key = str(value).strip().lower()
    if key in _ALIASES:
        return _ALIASES[key]
    raise ValueError(f"unknown tool policy: {value}")


def allows(name: str, policy: str) -> bool:
    level = normalize(policy)
    if level == POLICY_FULL:
        return True
    if level == POLICY_CHAT_ONLY:
        return False
    return name in READ_ONLY_TOOLS


def filter_tools(tools: Sequence[Any], policy: str) -> list[Any]:
    level = normalize(policy)
    if level == POLICY_FULL:
        return list(tools)
    return [t for t in tools if allows(getattr(t, "name", ""), level)]


def describe(level: str) -> str:
    level = normalize(level)
    if level == POLICY_FULL:
        return "完整权限：内置文件/执行工具 + MCP + 子代理编排 + GA"
    if level == POLICY_READ_ONLY:
        return "只读：file_read / list_files / load_skill；无写入、无执行、无 MCP、无编排、无 GA"
    return "纯对话：不注入任何工具"
