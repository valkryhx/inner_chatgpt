"""文件编辑的降级匹配：对齐 CC319 的 Edit 工具（`m0520:215-232` 的 `hWe`）。

## 为什么需要这一层

`file_patch` 的设计是「只发 diff」——模型只发 `old_content`（通常 1–3 行）
而不是整个文件，省 token、省生成时间。但它有一个致命的脆弱点：

**模型手抄的 `old_content` 与文件实际内容可能有细微差异。**

实测形态（本仓库与上游都遇到过）：

| 差异 | 怎么发生 | 精确匹配结果 |
| --- | --- | --- |
| 智能引号 vs ASCII | 模型从网页/文档读到 `“”`，写回时变成 `""` | 找不到 |
| `\\uXXXX` vs 实际字符 | 模型把中文写成 `\\u4f60`，文件里是「你」 | 找不到 |
| 实际字符 vs `\\uXXXX` | 文件是 JSON 源码，模型发「你」 | 找不到 |

一旦找不到，模型的反应是「重读文件 → 再抄错 → 再失败」，因为失败文案
只说了「未找到」。这正是「只发 diff」在 Hlaudz 上不生效的根因。

CC 的解法不是让模型抄得更准（做不到），而是**在匹配侧兜底**。

## 四级降级的顺序（照抄 CC，不要改顺序）

1. **精确**：`old in text`
2. **引号归一化**：两边都归一化后再找，然后在**原文**里截取等长片段
3. **old 里的 `\\uXXXX` 还原成字符**
4. **字符转成 `\\uXXXX` 再匹配**（用正则 + 反斜杠奇偶校验）

顺序不能变：精确优先保证「模型抄对了就走快路径」，归一化只在前者失败时兜底，
避免把本来能精确命中的内容改写成另一种形式。

## 返回值语义（关键）

返回**文件里真实存在的那段文本**（而不是布尔）。调用方用它做替换，
于是「模型抄错的部分」不会被写进文件。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 智能引号 → ASCII。CC 的 `m0520:135-141`。
_SMART_SINGLE_OPEN = "‘"    # ‘
_SMART_SINGLE_CLOSE = "’"   # ’
_SMART_DOUBLE_OPEN = "“"    # “
_SMART_DOUBLE_CLOSE = "”"   # ”


def _normalize_quotes(s: str) -> str:
    """把智能引号归一化成 ASCII 引号。

    长度不变（1 字符换 1 字符），所以归一化后的位置可以直接映射回原文。
    这个不变量是第 ② 级「在原文里截取等长片段」成立的前提。
    """
    return (s.replace(_SMART_SINGLE_OPEN, "'")
             .replace(_SMART_SINGLE_CLOSE, "'")
             .replace(_SMART_DOUBLE_OPEN, '"')
             .replace(_SMART_DOUBLE_CLOSE, '"'))


def _unescape_u(s: str) -> str:
    """把字面量 `\\uXXXX` 还原成实际字符。

    必须保护 `\\\\uXXXX`（已转义的反斜杠）：那种情况不是转义序列，
    还原会误伤。CC 的 `F()` 用 `(\\\\\\\\)` 分支做这件事（`m0520:144-146`）。
    """
    import re
    return re.sub(
        r"(\\\\)|\\u([0-9a-fA-F]{4})",
        lambda m: m.group(1) if m.group(1) is not None
        else chr(int(m.group(2), 16)),
        s)


def _backslashes_before(text: str, pos: int) -> int:
    """数 `pos` 前面连续反斜杠的个数。"""
    n = 0
    i = pos - 1
    while i >= 0 and text[i] == "\\":
        n += 1
        i -= 1
    return n


def _escaped_form_pattern(s: str) -> str | None:
    """把实际字符转成「匹配 `\\uXXXX` 形式」的正则。返回 None 表示不该走这级。

    对齐 CC 的 `X()`（`m0520:148-157`）：非 ASCII 字符变成
    `\\\\u[xx][XX]`（hex 大小写都接受），其它字符按正则转义。
    """
    import re
    if not any(ord(c) >= 128 for c in s):
        return None
    out = []
    for ch in s:
        if ord(ch) >= 128:
            hexs = format(ord(ch), "04x")
            body = "".join(
                f"[{h}{h.upper()}]" if ("a" <= h <= "f") else h for h in hexs)
            out.append(r"\\u" + body)
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _match_escaped(old: str, text: str) -> str | None:
    """第 ④ 级：old 是实际字符，text 里存的是 `\\uXXXX`。

    对齐 CC 的 `N()`（`m0520:160-181`）。三个要点：

    1. 若 old 末尾反斜杠数为奇数，说明 old 本身是截断的一段转义，放弃；
    2. 命中位置的前缀里，每个非 ASCII 字符对应的位置之前，
       反斜杠数必须为偶数（否则那个 `\\u` 是被 `\\\\` 转义的，不算真转义）；
    3. **要求唯一命中**：多处命中返回 None，让上层报「不唯一」而不是猜一个。
    """
    import re
    if not old:
        return None
    if _backslashes_before(old, len(old)) % 2 == 1:
        return None
    if "\\u" not in text:
        return None

    pattern = _escaped_form_pattern(old)
    if pattern is None:
        return None

    # 每个非 ASCII 字符在「转义形态」里的起始偏移（用于奇偶校验）
    offsets: list[int] = [0] if old[0] == "\\" else []
    pos = 0
    for ch in old:
        if ord(ch) >= 128:
            offsets.append(pos)
            pos += 6
        else:
            pos += 1

    try:
        rx = re.compile(pattern)
    except re.error as e:
        logger.info("Edit 转义匹配跳过：正则拒绝 %d 字符的模式（%s）",
                    len(pattern), type(e).__name__)
        return None

    first = None
    for m in rx.finditer(text):
        if not all(_backslashes_before(text, m.start() + off) % 2 == 0
                   for off in offsets):
            continue
        if first is None:
            first = m
            continue
        # 第二处命中 ⇒ 不唯一
        return None
    return first.group(0) if first is not None else None


def find_actual(old: str, text: str) -> str | None:
    """在 `text` 里找到与 `old` 等价的**实际文本**。找不到返回 None。

    返回的是文件里的原文，供调用方替换用。
    """
    if not old:
        return None

    # ① 精确
    if old in text:
        return old

    # ② 引号归一化（两边都归一化，再回原文截等长片段）
    norm_old = _normalize_quotes(old)
    norm_text = _normalize_quotes(text)
    idx = norm_text.find(norm_old)
    if idx != -1:
        return text[idx:idx + len(old)]

    # ③ old 里的 \uXXXX 还原
    unescaped = _unescape_u(old)
    if unescaped != old and unescaped in text:
        return unescaped

    # ④ old 是实际字符，text 里是 \uXXXX
    return _match_escaped(old, text)


def describe_failure(old: str, text: str) -> str:
    """生成「找不到」时的文案：必须告诉模型下一步做什么。

    CC 的文案（`m0591:212865`）在转义形态时会附加一句
    「也试过 \\uXXXX 互换，所以问题在别处，重读文件抄准确文本」。
    这一句很关键：不说的话模型会以为是引号问题，反复重试同一个错。
    """
    tried_escape = bool(_escaped_form_pattern(old) or _unescape_u(old) != old)
    tail = ""
    if tried_escape:
        tail = ("（已尝试 \\uXXXX 转义与智能引号的互换，仍未匹配，"
                "说明差异在别处：请重读该文件并逐字复制要替换的文本。）")
    return ("未找到匹配的旧文本块。" + tail +
            "请用 file_read 确认当前内容后，提供更长、逐字一致的 old_content。")