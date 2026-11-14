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
# memory_list / memory_read 是纯读：外部调用者看**本 token 所属用户**的记忆不构成越权
# （注入器与工具都按 token 绑定的 user_id 隔离），但 memory_write 是写，不进。
READ_ONLY_TOOLS = frozenset({
    "file_read", "list_files", "load_skill", "memory_list", "memory_read",
})

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


# --------------------------------------------------------------------------
# 记忆的两条旁路：它们**不经过 registry**，所以 `allows()` 管不到
# --------------------------------------------------------------------------
#
# 记忆有四个进出点，只有 ① 走 registry：
#
#   ① 模型调 memory_list / memory_read / memory_write   → registry（allows 覆盖）
#   ② 读侧注入（MemoryInjector / 选择器预取）            → 不走 registry
#   ③ 写侧后台提取（AgentLoop.on_turn_end 钩子）          → 不走 registry
#   ④ dream 巡检合并                                      → 服务端维护任务，与 token 无关
#
# ②③ 必须单独判，否则「chat_only 拿不到任何工具」和「chat_only 动不到任何东西」
# 是两回事 —— 这个区别在 2026-11-14 的 M5 实测里漏了：read_only / chat_only
# 的 token 仍然挂着后台提取钩子，等于拿到一条绕过 registry 的**写**通道，
# 而且写进去的内容会在下一个会话里被当成用户事实注入（用户看不见的持久副作用）。
#
# 三档的记忆语义（严格包含，逐档收窄）：
#
#   full       读 ✅  写 ✅   —— 与网页端完全一致
#   read_only  读 ✅  写 ❌   —— 「看得到的都看得到，但什么都别动」
#   chat_only  读 ❌  写 ❌   —— 「什么都不碰」。它的文案是「不注入任何工具」，
#                              把记忆注入也算进来才自洽：否则最严的一档反而
#                              能看到用户最私人的内容，阶梯就断了。
#
# 为什么 read_only 保留读：读的边界是 user_id，token 已经把它绑死了，
# 读自己用户的记忆不构成越权（对照 CC：MCP 记忆库在只读场景同样可读不可写）。
MEMORY_READ_POLICIES = frozenset({POLICY_FULL, POLICY_READ_ONLY})
MEMORY_WRITE_POLICIES = frozenset({POLICY_FULL})


def allows_memory_read(policy: str) -> bool:
    """记忆**读侧**（注入、选择器预取）是否生效。"""
    return normalize(policy) in MEMORY_READ_POLICIES


def allows_memory_write(policy: str) -> bool:
    """记忆**写侧**（后台提取）是否生效。"""
    return normalize(policy) in MEMORY_WRITE_POLICIES


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
        return ("只读：file_read / list_files / load_skill / memory_list / memory_read；"
                "无写入、无记忆写入、无执行、无 MCP、无编排、无 GA")
    return "纯对话：不注入任何工具，也不注入记忆"
