r"""RED：先读必写门禁必须被删除。

## 依据（外部证据，不是我们自己的判断）

`D:\git_codes\system_prompts_leaks\Anthropic\claude-code\claude-code-fable-5.2.md:133`：

> The **Edit** tool's description says you must Read a file before editing it.
> That no longer holds for files inside the working directory:
> **Edit works there without a prior Read.**
> When you already know the exact text to replace, for example from grep or
> cat output, edit directly.

`desktop-fable-5.2` 同样有这一条；5.0 / 5.1 / opus-4.7 / haiku-5.5 没有 ——
是较新版本才加的 override。

## 真机证据（为什么这条必须删）

2026-11-20 跑 Agent Team（session `co_8abfb27422c8`）：

- `writer_c` 的 `file_write` 被拒，它自己的原话是
  「上一条 file_write 因为『只读了片段』被硬门禁拒绝」
- 根因是 `DEFAULT_READ_LINES = 200`，而子代理要覆盖的报告有 22–45 KB。
  **一次 `file_read` 永远读不全** ⇒ `complete` 恒为 `False` ⇒ 覆盖写恒被拒
- 结果：researcher 10 轮 stopped、writer 起了 3 个（b/c），任务最终
  `pending`，前端看起来就是「卡住」

这不是「偶尔不方便」，是对大文件**必然失败**的门槛。

## 删掉之后靠什么保证安全

1. 路径安全层（`safe_workspace_path` / `resolve_in_workspace`）不动 ——
   越界仍然拒绝。这是真正的安全边界。
2. `file_patch` 的 `old_content` 必须**唯一命中**，否则报错 ——
   「改错地方」在语义上仍然难以发生。
3. 工具描述里保留「覆盖前应确认当前内容」的建议（prompt 层），
   但不再是硬门禁（代码层）。

> 注意：Write 工具的描述**没有**被那条 override 覆盖
> （`:3327` 仍写 "Overwriting an existing file you haven't Read will fail"）。
> 用户明确要求删掉整个门禁，按用户决定执行；差异已记录在
> `docs/20261120-删除写前必读门禁.md`。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

UID = "u_gate_test"


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    from app import config as app_config
    from app import workspace as ws_mod

    root = tmp_path / "u_gate_test"
    root.mkdir()
    monkeypatch.setattr(ws_mod, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(app_config, "WORKSPACES_DIR", tmp_path)
    return root


def _tools():
    from app.builtin_tools import build_builtin_tools

    return {t.name: t for t in build_builtin_tools(user_id=UID)}


def test_overwrite_existing_without_read_is_allowed(ws):
    """★ 核心：覆盖一个**没读过**的已存在文件，必须成功。"""
    (ws / "a.txt").write_text("old\n", encoding="utf-8")
    out = asyncio.run(_tools()["file_write"].call(
        {"path": "a.txt", "content": "new\n"}))
    assert "已写入" in out, f"仍被门禁拦住：{out}"
    assert (ws / "a.txt").read_text(encoding="utf-8") == "new\n"


def test_overwrite_large_unread_file_is_allowed(ws):
    """大文件（> 200 行 = DEFAULT_READ_LINES）覆盖也必须成功。

    这正是 writer_c 卡住的形态：报告 22–45 KB，一次 file_read 读不全。
    """
    big = "\n".join(f"line {i}" for i in range(1000)) + "\n"
    (ws / "big.md").write_text(big, encoding="utf-8")
    out = asyncio.run(_tools()["file_write"].call(
        {"path": "big.md", "content": "# 新报告\n"}))
    assert "已写入" in out, f"大文件覆盖仍被拦：{out}"
    assert (ws / "big.md").read_text(encoding="utf-8") == "# 新报告\n"


def test_partial_read_then_overwrite_is_allowed(ws):
    """只读片段之后覆盖写，也必须成功（旧门禁正是在这里拒绝）。"""
    (ws / "a.txt").write_text("x\n" * 500, encoding="utf-8")
    tools = _tools()
    asyncio.run(tools["file_read"].call({"path": "a.txt", "count": 10}))
    out = asyncio.run(tools["file_write"].call({"path": "a.txt", "content": "z\n"}))
    assert "已写入" in out, f"读了片段仍被拦：{out}"


def test_patch_without_prior_read_is_allowed(ws):
    """`file_patch` 也不再要求先读 —— 这正是提示词 override 明说的那条。

    Edit 的读前置被 `claude-code-fable-5.2.md:133` 明确推翻。
    """
    (ws / "a.txt").write_text("old value\n", encoding="utf-8")
    out = asyncio.run(_tools()["file_patch"].call(
        {"path": "a.txt", "old_content": "old", "new_content": "new"}))
    # 成功文案是「已局部修改」（内置工具与沙箱服务两侧一致）。
    # 别手抄文案的另一半 —— 上一版写成「已修改」，探针自己判错。
    assert "已局部修改" in out, f"patch 被门禁拦住：{out}"
    assert (ws / "a.txt").read_text(encoding="utf-8") == "new value\n"


def test_patch_still_requires_unique_match(ws):
    """删掉门禁**不**等于放松唯一性 —— 多处命中仍然要拒绝。

    这是「改错地方」的真正防线，与读前置无关。
    """
    (ws / "a.txt").write_text("dup\ndup\n", encoding="utf-8")
    out = asyncio.run(_tools()["file_patch"].call(
        {"path": "a.txt", "old_content": "dup", "new_content": "x"}))
    assert "无法确定唯一位置" in out or "处匹配" in out, f"多处命中没有被拒：{out}"


def test_path_escape_still_rejected(ws):
    """删门禁**不**等于放松路径安全。"""
    out = asyncio.run(_tools()["file_write"].call(
        {"path": "../../etc/passwd", "content": "x"}))
    assert "已写入" not in out


def test_missing_file_patch_still_errors(ws):
    out = asyncio.run(_tools()["file_patch"].call(
        {"path": "nope.txt", "old_content": "a", "new_content": "b"}))
    assert "不存在" in out

def test_tool_descriptions_do_not_claim_a_hard_gate():
    """工具描述不许再宣称「必须先读，否则被拒」。

    ## 为什么这条也要测

    代码里删了门禁，但描述里还留着「硬门禁」字样，模型会**以为**
    有这个限制，从而多花一轮 file_read、或者以为自己被拒是别的原因。
    文案与行为不一致，比两边都严或都松都难查 —— 用户看到的是模型
    "莫名其妙先读一遍再写"。

    真机佐证：writer_c 在被拒之后的原话是在解释门禁行为，
    如果门禁还在而描述不提，它会反复试；反过来描述提而门禁没了，
    它会做无谓的额外步骤。
    """
    from app.builtin_tools import build_builtin_tools

    tools = {t.name: t for t in build_builtin_tools(user_id=UID)}
    for name in ("file_write", "file_patch"):
        desc = tools[name].description
        assert "硬门禁" not in desc, f"{name} 的描述仍宣称硬门禁：{desc}"
        assert "写入前必读" not in desc, f"{name} 的描述仍宣称写入前必读：{desc}"
