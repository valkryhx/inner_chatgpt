"""file_write 的工具描述必须引导「长内容分段写」。

## 为什么要这条（真机定量证据）

2026-11-20 会话 `co_891bb2fd3751` 的时间轴实测：

| 时段 | 时长 | 字符数 | 速率 |
| --- | --- | --- | --- |
| researcher 写底稿 | 76.6s | 14,025 | 183 字符/秒 |
| writer 写报告 | 95.6s | 27,495 | 288 字符/秒 |

两次 `file_write` 本身耗时 **0 秒**，而生成要写入的内容分别花了
76 秒和 96 秒。会话里最大的一段静默是 **53.1 秒**，恰好卡在
「模型说『现在开始写底稿』」之后、`file_write` 调用之前。

`file_write` 已支持 `mode="append"`（`builtin_tools.py:261`），
所以分段写不需要改实现，**只需要在工具描述里明确引导**。
否则模型默认一次写完，用户就要盯着 50+ 秒的空白。

## 关于 CC 的做法（不是「分段写」，但目的相同）

查 `reconstructed319` 后确认：CC 的 Write 描述里**没有**任何「分段写」措辞
（`m0446_0x11a5dc93.js:16-27`）。它的做法是另一条：

> `Prefer the Edit tool for modifying existing files — it only sends the diff.
>  Only use this tool to create new files or for complete rewrites.`

即：**新建用 Write 一次写，修改用 Edit 只发差异**。
两者共同的效果都是「单次输出尽量小」。Hlaudz 缺的是「新建大文件」这一半的引导
—— 而这正是本次 96 秒空白的场景。

## 判据

描述里必须同时出现：
1. `append` 这个具体参数名（否则模型不知道有这条路）
2. 一个可执行的字数分界（否则「长」是主观的）
3. 「分段」与「一次性」的因果（否则模型不会认为这是为自己好）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.builtin_tools import build_builtin_tools  # noqa: E402


def _write_desc() -> str:
    tools = build_builtin_tools("u_test_desc")
    for t in tools:
        if getattr(t, "name", "") == "file_write":
            return getattr(t, "description", "")
    raise AssertionError("找不到 file_write 工具")


def test_desc_mentions_append_mode():
    d = _write_desc()
    assert "append" in d, (
        "描述里没提 append —— 模型不会知道可以分段追加写入。"
        f"当前描述：{d}")


def test_desc_gives_concrete_size_threshold():
    """必须有可执行的字数分界，不能只说「长内容」。"""
    d = _write_desc()
    import re
    # 接受 2000 / 3000 / 约 3000 字 这类表述
    assert re.search(r"[约>≥]\s*\d{3,5}\s*字|超过\s*\d{3,5}\s*字|\d{3,5}\s*字以上", d), (
        "描述里没有具体字数分界 —— 「长」是主观的，模型不会照做。"
        f"当前描述：{d}")


def test_desc_explains_why_chunking_helps():
    """要说清「为什么」，否则模型会当作可选建议忽略。"""
    d = _write_desc()
    assert ("一次" in d and ("空" in d or "等" in d)), (
        "描述没说清「一次性写完会让用户长时间看到空白」——"
        "不讲原因的引导模型不会遵守。"
        f"当前描述：{d}")
