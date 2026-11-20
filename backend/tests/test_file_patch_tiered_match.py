"""file_patch 的降级匹配（对齐 CC319 的 Edit）。

## 为什么需要

现有 `file_patch` 是**精确子串匹配**：`full.count(old_content)`。
模型只要抄错一个引号，就得到「未找到匹配的旧文本块」，然后重读文件、
再抄错、再失败——这是「只发 diff」这个设计在 Hlaudz 上不生效的根因。

CC319 的 Edit 用四级降级（`m0520:215-232`）：

    ① 精确
    ② 引号归一化后再匹配（智能引号 ↔ ASCII）
    ③ old_string 里的 \\uXXXX 还原成实际字符再匹配
    ④ 实际字符转成 \\uXXXX 形式再匹配（正则，需反斜杠奇偶校验）

`hWe()` 返回**文件里真实存在的那段文本**，所以后续替换用的是原文，
不是模型抄错的那份。

## 判据设计

每条测试都必须在「有降级」与「无降级」两种状态下读数不同：
- 降级生效：返回成功，且**文件内容正确**
- 降级失效：返回「未找到」

只断言「返回值里没有错误字样」不够——旧实现也可能因为别的原因成功。
所以同时断言**修改后的文件内容**。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import builtin_tools as bt  # noqa: E402


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """把 user_workspace 指到 tmp，避免碰真实数据。"""
    monkeypatch.setattr(bt, "user_workspace", lambda uid, create=True: tmp_path)
    monkeypatch.setattr(bt, "safe_workspace_path",
                        lambda uid, p, root="": tmp_path / str(p).lstrip("/"))
    monkeypatch.setattr(bt, "display_path", lambda uid, t: str(t))
    return tmp_path


def _write(ws, name, text):
    p = ws / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


# --------------------------------------------------------------------------
# ① 精确匹配（回归：必须仍然工作）
# --------------------------------------------------------------------------

def test_exact_match_still_works(ws):
    _write(ws, "a.txt", "hello world\nsecond line\n")
    out = bt._patch_impl("u1", "a.txt", "hello world", "goodbye world")
    assert "已局部修改" in out, out
    assert (ws / "a.txt").read_text(encoding="utf-8") == "goodbye world\nsecond line\n"


# --------------------------------------------------------------------------
# ② 引号归一化
# --------------------------------------------------------------------------

def test_smart_quotes_in_file_match_ascii_in_old_string(ws):
    """文件里是智能引号，模型发的是 ASCII —— 必须能匹配。

    这是最常见的失败形态：模型从网页/文档里读到的文本带弯引号，
    写回时变成 ASCII，于是精确匹配失败。
    """
    _write(ws, "b.py", 'msg = “你好”\nprint(msg)\n')
    out = bt._patch_impl("u1", "b.py", 'msg = "你好"', 'msg = "再见"')
    assert "已局部修改" in out, f"智能引号未归一化：{out}"
    content = (ws / "b.py").read_text(encoding="utf-8")
    assert 'msg = "再见"' in content, content


def test_ascii_in_file_matches_smart_quotes_in_old_string(ws):
    """反向：文件里是 ASCII，模型发来智能引号。"""
    _write(ws, "c.py", 'msg = "你好"\n')
    out = bt._patch_impl("u1", "c.py", 'msg = “你好”', 'msg = "x"')
    assert "已局部修改" in out, f"反向引号归一化失败：{out}"


def test_quote_normalization_preserves_other_content(ws):
    """归一化只能改引号，不能把文件里其它内容也改掉。"""
    original = 'a = "keep"\nb = “change”\nc = "keep2"\n'
    _write(ws, "d.py", original)
    bt._patch_impl("u1", "d.py", 'b = "change"', 'b = "done"')
    content = (ws / "d.py").read_text(encoding="utf-8")
    assert 'a = "keep"' in content, content
    assert 'c = "keep2"' in content, content
    assert 'b = "done"' in content, content


# --------------------------------------------------------------------------
# ③ old_string 里是 \\uXXXX，文件里是实际字符
# --------------------------------------------------------------------------

def test_unicode_escape_in_old_string_matches_real_chars(ws):
    """模型把中文写成 \\u4f60\\u597d，文件里是「你好」。

    ⚠️ 这里必须用**真正的字面量反斜杠**。首版写成 r"const s = '你好';"
    —— 那个 raw string 里其实已经是中文字符了？不是：raw string 里 `你`
    确实是 6 个字面字符。问题在最初这版是 `_write(..., "const s = '你好';")`
    配 `r"const s = '你好';"`，两边都是真中文，测的根本是精确匹配。
    现在两边都写成转义字面量 vs 真中文，才是真的第 ③ 级。
    """
    _write(ws, "e.js", "const s = '你好';\n")     # 文件里是真实中文
    # old_content 是**字面量** `你好`（不是中文）
    old_literal = "const s = '\\u4f60\\u597d';"
    assert "\\u" in old_literal, "测试自身写错了：old_content 里没有字面转义"
    out = bt._patch_impl("u1", "e.js", old_literal, "const s = 'ZZZ';")
    assert "已局部修改" in out, f"\\uXXXX 还原失败：{out}"
    assert "ZZZ" in (ws / "e.js").read_text(encoding="utf-8")


def test_double_escaped_backslash_is_not_converted(ws):
    """`\\\\u4f60`（转义了的反斜杠）不能被当成转义序列还原。

    这是 CC 在 `F()` 里 `(\\\\\\\\)` 分支防的坑：双重转义误伤。
    """
    _write(ws, "f.js", r"const s = '\\u4f60';" + "\n")
    # old_string 与文件一致（真的是双反斜杠），应走精确匹配
    out = bt._patch_impl("u1", "f.js", r"const s = '\\u4f60';", r"const s = 'X';")
    assert "已局部修改" in out, out
    assert r"'X'" in (ws / "f.js").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# ④ 文件里是 \\uXXXX，old_string 是实际字符
# --------------------------------------------------------------------------

def test_real_chars_in_old_string_match_escaped_file(ws):
    """文件里存的是 \\u4f60（例如 JSON 源码），模型发「你」。"""
    _write(ws, "g.json", '{"k": "\\u4f60\\u597d"}\n')
    out = bt._patch_impl("u1", "g.json", '"你好"', '"再见"')
    assert "已局部修改" in out, f"字符→转义匹配失败：{out}"
    content = (ws / "g.json").read_text(encoding="utf-8")
    assert "再见" in content, content


def test_escaped_match_keeps_hex_case_style(ws):
    """写回时跟随原文件的 hex 大小写风格（对齐 CC 的 `Mmr`，`m0520:189-209`）。

    首版测试写成文件里同时有 `\\u4F60` 和真实「你」，于是精确匹配就命中了
    真实那个 —— 根本没走到第 ④ 级，测了个寂寞。

    现在文件里**只有**转义形态（大写 hex），old_content 给真实字符，
    必须走第 ④ 级才能成功。
    """
    _write(ws, "h.json", '{"a": "\\u4F60\\u597D"}\n')   # 只有转义形态，大写
    out = bt._patch_impl("u1", "h.json", '"你好"', '"X"')
    assert "已局部修改" in out, f"字符→转义匹配失败：{out}"
    content = (ws / "h.json").read_text(encoding="utf-8")
    assert '"X"' in content, content
    # 其余未被替换的转义必须原样保留（这是一条弱断言，仅防「整文件被重写」）


# --------------------------------------------------------------------------
# replace_all
# --------------------------------------------------------------------------

def test_replace_all_replaces_every_occurrence(ws):
    _write(ws, "i.txt", "foo\nfoo\nfoo\n")
    out = bt._patch_impl("u1", "i.txt", "foo", "bar", replace_all=True)
    assert "已局部修改" in out, out
    assert (ws / "i.txt").read_text(encoding="utf-8") == "bar\nbar\nbar\n"


def test_without_replace_all_multiple_matches_still_rejected(ws):
    """唯一性检查不能因为加了 replace_all 就失效。"""
    _write(ws, "j.txt", "foo\nfoo\n")
    out = bt._patch_impl("u1", "j.txt", "foo", "bar")
    assert "处匹配" in out or "唯一" in out, f"应拒绝多处匹配：{out}"
    assert (ws / "j.txt").read_text(encoding="utf-8") == "foo\nfoo\n"


# --------------------------------------------------------------------------
# 失败文案必须教模型下一步（对齐 CC）
# --------------------------------------------------------------------------

def test_not_found_message_mentions_escape_attempt_when_applicable(ws):
    """转义形态找不到时，要说明「也试过 \\uXXXX 互换」。

    否则模型会以为是别的引号问题，反复重试同一个错。
    """
    _write(ws, "k.js", "const x = 1;\n")
    out = bt._patch_impl("u1", "k.js", r"const s = '你好';", "x")
    assert "未找到" in out or "找不到" in out, out
    assert "\\u" in out or "转义" in out, (
        f"失败文案没提转义互换，模型会重复同一个错误：{out}")


def test_not_found_message_is_actionable(ws):
    """一般找不到时也要给出可执行下一步。"""
    _write(ws, "l.txt", "aaa\n")
    out = bt._patch_impl("u1", "l.txt", "zzz", "yyy")
    assert "file_read" in out or "重读" in out, (
        f"失败文案没说该做什么：{out}")


# --------------------------------------------------------------------------
# 安全边界不能破
# --------------------------------------------------------------------------

def test_does_not_escape_workspace(ws):
    out = bt._patch_impl("u1", "../../etc/passwd", "a", "b")
    # 允许「文件不存在」，但绝不能真的读到外面
    assert "已局部修改" not in out