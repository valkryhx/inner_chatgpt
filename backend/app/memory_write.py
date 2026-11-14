"""写侧（M4）：四道闸门的判定逻辑。

本模块目前只实现**闸门与提示词**，不含「派后台子代理」的编排 ——
那部分要接 `SubagentHost`，属于 M4 的第二半。先让闸门可独立测通，
是因为闸 2（prose 计数）有一个**中文产品必须专门测**的陷阱（见下）。

四道闸门照抄 CC 的**文件模式**路径（`Le()`，`m0832:922`–`:1050`）。
v3 §2.5 复核过归属：闸 1–3 通用、闸 4 仅文件模式、闸 5–8 仅 MCP。
Hlaudz 是文件模式，所以只有 1–4。**没有 `first_look`** ——
它是 MCP 模式专属的第 8 道闸，文件模式下 CC 根本没有。

| 闸 | 条件 | 跳过后果 |
|---|---|---|
| 1 | 本区间内主代理已直写过记忆 | 与直写打架、重复写 |
| 2 | 最近窗口内自然语言不足 | tool-only 轮次（跑测试、读文件）不值得记 |
| 3 | 每 N 轮才跑一次 | 每轮都跑会显著加钱 |
| 4 | 写入目标会被权限规则拒绝 | 派了子代理也写不进去 |
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from . import memory_store as ms

# 中日韩字符（含日文假名、泰文 —— 与 CC 的 Et 系数适用面一致）
_CJK_RE = re.compile(r"[一-鿿぀-ヿぁ-ゟ゠-ヿ฀-๿]")

# 闸 3 的默认频率。CC 文件模式 `vt = 3`（`m0832:218`）。
DEFAULT_EVERY_N_TURNS = 3

# 闸 2 的默认阈值。CC 文件模式是 3（不是 50 —— 50 是 MCP 模式 schema 的上限）。
DEFAULT_MIN_PROSE_WORDS = 3

# 「直写」的判定：主代理在本区间内调过这些工具
DIRECT_WRITE_TOOLS = frozenset({"memory_write"})


def prose_word_count(text: str) -> int:
    """英文按空白分词 + 中日韩字符按 **2 字算 1 词**。

    为什么必须这么做：``minProseWords=50`` 时，一段 200 字的中文对话用
    ``split()`` 只切出 1 个元素，会被判成「不足 50 词」—— **记忆永远不写**。
    CC 的实现是 ``Pe(e) >= r || Tt(e) >= r * Et``，其中 ``Et = 2``。

    这个陷阱在单测里很容易漏（拿英文测全绿），必须专门有一条中文用例锁住。
    """
    if not text:
        return 0
    latin = len([w for w in text.split() if w.strip()])
    cjk = len(_CJK_RE.findall(text))
    return latin + cjk // 2


def message_text_of(msg: dict[str, Any]) -> str:
    """把一条 history 消息里的可读文本取出来（正文 + tool 结果的文本部分）。"""
    parts: list[str] = []
    content = msg.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
    return "\n".join(parts)


def already_wrote_in_range(messages: Iterable[dict[str, Any]]) -> bool:
    """闸 1：本区间内主代理是否已经**直接**写过记忆。

    为什么要这道闸：主代理当轮明确决定写了一条记忆，后台提取器不该再对
    同一段对话做一次「猜测式」提取 —— 两者目标重叠，产生的条目往往互相重复，
    而 dream 要等到 24 小时后才合并。
    """
    for msg in messages or []:
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            if str(fn.get("name") or "") in DIRECT_WRITE_TOOLS:
                return True
    return False


def has_enough_prose(messages: Iterable[dict[str, Any]], *,
                     min_words: Optional[int] = None) -> bool:
    """闸 2：最近窗口内自然语言是否足够。

    只数**用户与助手**的正文，不数 tool 结果：跑一次测试的输出来有两千行，
    但它不含任何「值得记的事」。
    """
    threshold = DEFAULT_MIN_PROSE_WORDS if min_words is None else int(min_words)
    total = 0
    for msg in messages or []:
        if str(msg.get("role") or "") not in ("user", "assistant"):
            continue
        if msg.get("_volatile"):
            continue
        total += prose_word_count(message_text_of(msg))
    return total >= threshold


def every_n_turns() -> int:
    """闸 3 的频率。``HLZ_MEMORY_EVERY_N_TURNS``，默认 3。"""
    return ms._env_int("HLZ_MEMORY_EVERY_N_TURNS", DEFAULT_EVERY_N_TURNS, lo=1)


def min_prose_words() -> int:
    return ms._env_int("HLZ_MEMORY_MIN_PROSE_WORDS", DEFAULT_MIN_PROSE_WORDS, lo=1)


def turn_gate_open(turn_count: int, *, every_n: Optional[int] = None) -> bool:
    """闸 3：每 N 轮才开一次。

    ``every_n=1`` 时每轮都开；``=3`` 时第 3、6、9… 轮开。
    用 ``% == 0`` 而不是 ``> = n`` 累积计数：后者在重启后计数归零会退化成
    「永远等不到」，前者只看绝对轮次，语义更简单，也更好测。
    """
    n = max(1, every_n if every_n is not None else every_n_turns())
    return int(turn_count) > 0 and int(turn_count) % n == 0


@dataclass
class GateDecision:
    """闸门结论。``allowed=False`` 时 ``reason`` 说明是哪道闸挡的（日志/审计用）。"""

    allowed: bool
    reason: str = ""
    gate: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def evaluate_gates(messages: list[dict[str, Any]], *, turn_count: int,
                   every_n: Optional[int] = None,
                   min_words: Optional[int] = None,
                   permission_ok: bool = True,
                   enabled: Optional[bool] = None,
                   hard_disabled: Optional[bool] = None) -> GateDecision:
    """按 CC 的顺序跑四道闸，返回第一个挡住的理由。

    顺序照抄 CC（`:938` 直写 → `:950` prose → `:963` 频率 → `:976` 权限）。
    顺序本身有信息：闸 1 最便宜（扫 tool_calls），闸 4 最贵（要查规则），
    便宜的先跑能省掉后面的开销。
    """
    if enabled is None:
        enabled = ms.auto_write_enabled()
    if hard_disabled is None:
        hard_disabled = ms.memory_hard_disabled()
    if hard_disabled:
        return GateDecision(False, "HLZ_MEMORY_DISABLED=1（排障出口，等价 CC 的 settingsHooksOff）",
                            gate="disabled")
    if not enabled:
        return GateDecision(False, "HLZ_MEMORY_AUTO_WRITE=0（自动提取默认关闭）", gate="disabled")

    if already_wrote_in_range(messages):
        return GateDecision(False, "本区间内主代理已直写记忆（闸 1）", gate="direct-write")
    if not has_enough_prose(messages, min_words=min_words):
        return GateDecision(
            False,
            f"最近窗口自然语言不足 {min_words if min_words is not None else min_prose_words()} 词（闸 2）",
            gate="prose")
    if not turn_gate_open(turn_count, every_n=every_n):
        return GateDecision(
            False,
            f"未到提取频率（每 {every_n if every_n is not None else every_n_turns()} 轮，闸 3）",
            gate="frequency")
    if not permission_ok:
        return GateDecision(False, "写入目标会被权限规则拒绝（闸 4）", gate="permission")
    return GateDecision(True)


# ---------------- 提取子代理的提示词 ----------------

EXTRACT_PROMPT = """你是记忆提取器。读下面这段对话，判断其中有没有**跨会话仍然有用**的事实，
写进用户的长期记忆库。

## 该写什么

- `user`：这个人的称呼、角色、语言偏好、长期口味（「叫我老徐」「始终用中文」）
- `feedback`：他对我工作方式的**纠正**或**确认**（「别用表格」「这样分步很好」）
- `project`：跨会话的项目状态（做到哪一步、遗留什么、卡在哪）
- `reference`：外部资源指针（URL、看板地址、工单号）

## 不该写什么

- 本次对话的临时内容（「帮我改这个函数」）—— 下一轮就没意义了
- 你自己推测或补充的信息 —— 只记用户明确说过的
- 已有记忆的重复条目 —— 先 `memory_list` 看一眼；同一条要更新就用
  `memory_write` 带上它的 `if_version`，不要新建一条讲同一件事的
- 敏感信息（密码、密钥、证件号）—— 一律不写

## 怎么写

每条记忆给：
- `slug`：小写字母/数字 + `-_.`，语义化，如 `user-name-pref`、`proj-memory-status`
- `title`：短标题
- `description`：**一句话说清这条记的是什么**。未来靠它判断相关性，
  所以「用户偏好」这种空话没用，要写「用户希望被叫老徐」。
- `content`：正文（markdown），把细节写全
- `kind`：上面四类之一

如果没有值得记的，**不要写任何东西**，直接回复「无」。宁可漏记，不要乱记 ——
错误记忆会在下一个会话里被当成事实注入，比没记住坏得多。

## 对话

{conversation}
"""


def build_extract_prompt(conversation: str, *, max_chars: int = 12000) -> str:
    """拼提取提示词。对话过长时保留**头部与尾部**。

    为什么不只留尾部：用户常常在开头交代偏好（「以后都用中文」），
    那正是最该记的部分，只留尾部会把它丢掉。
    """
    text = conversation or ""
    if len(text) > max_chars:
        head = text[: max_chars // 3]
        tail = text[-(max_chars * 2 // 3):]
        text = f"{head}\n\n…（中间省略 {len(text) - len(head) - len(tail)} 字）…\n\n{tail}"
    return EXTRACT_PROMPT.replace("{conversation}", text)


def conversation_from(messages: Iterable[dict[str, Any]], *, limit: int = 40) -> str:
    """把 history 渲染成给提取器读的对话文本（只取最后 ``limit`` 条）。"""
    rows: list[str] = []
    items = [m for m in messages or [] if isinstance(m, dict)]
    for msg in items[-limit:]:
        role = str(msg.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        if msg.get("_volatile"):
            continue  # 注入块不是对话内容
        text = message_text_of(msg).strip()
        if not text:
            continue
        label = "用户" if role == "user" else "助手"
        rows.append(f"{label}：{text}")
    return "\n\n".join(rows)
