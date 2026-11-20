"""每一轮 agent 动作都要留下「时间点 + 50 字以内摘要」。

## 为什么需要

2026-11-20 跑了三次 agent team，耗时 5.4 / 17.9 / 24.4 分钟。事后要回答
一个最基本的问题——**这些时间是"持久性好"还是"响应慢"？**——却发现
根本无从回答：日志里只有每轮 `turns` 计数和最后一行 total，
**没有任何一条记录说明"第 N 秒时 agent 在做什么"**。

于是出现两种完全相反的解释都能自圆其说：
- 「它一直在干活，只是活多」——持久性好
- 「它卡了很多次，只是最后凑巧成功」——响应慢

要区分这两者，必须能画出时间轴：**每个动作发生在第几毫秒、是什么动作**。

## 判据设计

不能只断言「有没有日志行」——日志行在两种状态下都存在（别的模块在打）。
必须断言三件事同时成立，这三件在「慢」和「卡」两种状态下读数不同：

1. **有 monotonically 递增的时间偏移**（能排出先后与间隔）
2. **有动作名**（是 `tool_start` 还是 `text_delta`）
3. **有 50 字以内的参数摘要**（能看出"在搜什么"而不是只有"搜过"）
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import activity_trace as at  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    at.reset_for_test()
    yield
    at.reset_for_test()


def test_records_offset_action_and_summary():
    """一条事件必须同时带 t(ms) / action / brief。"""
    tr = at.ActivityTrace(generation_id="g_test", turn_label="root")
    tr.record("tool_start", name="web_search_exa",
              detail={"query": "特朗普 2026 政策"})
    rows = at.drain_for_test()
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "tool_start"
    assert r["t"] >= 0
    assert "web_search_exa" in r["brief"]
    assert "特朗普" in r["brief"]


def test_offsets_are_monotonic():
    """时间偏移必须单调不减——否则无法用它判「间隔多久」。"""
    tr = at.ActivityTrace(generation_id="g2")
    for i in range(5):
        tr.record("text_delta", detail={"text": f"chunk{i}"})
    rows = at.drain_for_test()
    ts = [r["t"] for r in rows]
    assert ts == sorted(ts), f"时间偏移非单调：{ts}"


def test_brief_is_bounded():
    """摘要必须截断到 50 字以内，否则日志会被 tool 参数淹没。

    这条不是"美观"要求：tool 参数里可能有整个文件内容，
    不截断会让单行日志到几十 KB，`grep` 直接不可用。
    """
    tr = at.ActivityTrace(generation_id="g3")
    tr.record("tool_start", name="file_write",
              detail={"content": "x" * 5000})
    brief = at.drain_for_test()[0]["brief"]
    # 允许动作名前缀，但正文部分不能是 5000 字
    assert len(brief) <= 120, f"摘要太长（{len(brief)} 字）：{brief[:80]}"


def test_text_delta_is_aggregated_not_per_char():
    """流式 text_delta 不能每帧一条——那会写出几十万行。

    但**聚合后必须仍能看出「这一批文字什么时候到」**，
    所以聚合是按时间窗，不是丢弃；`flush()` 是窗外的落盘点。
    """
    tr = at.ActivityTrace(generation_id="g4")
    for i in range(300):
        tr.record("text_delta", detail={"text": "字"})
    tr.flush()          # 收尾落盘（生产里在 finally 调用）
    rows = at.drain_for_test()
    assert len(rows) < 300, f"每帧一条，共 {len(rows)} 行——日志会被淹没"
    assert len(rows) >= 1, "flush 之后仍然一行都没有 ⇒ 这批文字被丢弃了"


def test_no_trace_when_disabled(monkeypatch):
    """关闭时必须一行都不写（性能路径）。"""
    monkeypatch.setenv("HLZ_ACTIVITY_TRACE", "0")
    at.reset_for_test()
    tr = at.ActivityTrace(generation_id="g5")
    tr.record("tool_start", name="x", detail={})
    assert at.drain_for_test() == []


def test_line_is_greppable_and_has_generation():
    """落盘行必须能被 grep 到 generation_id，且带 t= 前缀。

    这是事后把某次运行的完整时间轴捞出来的唯一方式。
    """
    tr = at.ActivityTrace(generation_id="g_abc123", turn_label="root")
    tr.record("tool_start", name="code_run", detail={"code": "print(1)"})
    line = at.render_line_for_test(at.drain_for_test()[0], "g_abc123", "root")
    assert "g_abc123" in line
    assert re.search(r"t=\d+ms", line), f"缺时间偏移标记：{line}"
    assert "code_run" in line


def test_sub_prefix_streaming_is_aggregated():
    """子代理的流式事件（`sub.text_delta` / `sub.thinking_delta`）同样要聚合。

    ## 这是真机抓出来的缺陷，不是假想

    首版 `record()` 只认裸 `text_delta`，于是子代理的 `sub.thinking_delta`
    每帧一条。实测一次运行里 60 秒就写了几百行，全是
    "I have enough from" / "two searches in" 这种半句 ——
    时间轴被碎片淹没，反而看不出「哪一段慢」。
    """
    tr = at.ActivityTrace(generation_id="g_sub")
    for i in range(200):
        tr.record("sub.thinking_delta", name="researcher",
                  detail={"text": "想"})
    tr.flush()
    rows = at.drain_for_test()
    assert len(rows) < 10, f"子代理流式被逐帧记录，共 {len(rows)} 行"


def test_container_event_is_skipped():
    """`subagent_event` 容器事件本身不记 —— 内容由内部 `sub.*` 表达。

    实测一次运行里约 1/3 的行是 `action=subagent_event brief=`（空），
    纯噪音，会把真正有信息量的动作挤出 grep 视野。
    """
    tr = at.ActivityTrace(generation_id="g_cont")
    tr.record("subagent_event", detail=None)
    tr.record("sub.tool_start", name="web_search_exa", detail={"query": "x"})
    rows = at.drain_for_test()
    actions = [r["action"] for r in rows]
    assert "subagent_event" not in actions, f"容器事件未被跳过：{actions}"
    assert "sub.tool_start" in actions


def test_streaming_detail_as_plain_string_is_counted():
    """★ 接入侧传的是**裸字符串**，不是 dict —— 必须也能统计字数。

    ## 这是真机 run2 抓出来的回归

    `routers/chat.py` 的调用是：

        trace.record(et, name=..., detail=ev.get("input") or ev.get("text"))

    `text_delta` 没有 `input`，所以 `detail` 落到 `ev.get("text")` —— 一个
    **字符串**。而首版 `record()` 里写的是：

        text = (detail or {}).get("text", "") if isinstance(detail, dict) else ""

    字符串不是 dict ⇒ `text` 恒为 `""` ⇒ 累加 0 字 ⇒ 聚合永远不触发。

    后果：run2 的时间轴 185 行里**一条 `+N 字` 都没有**，
    整轮的 5 万多字符产出在日志里完全消失。这比不聚合更糟——
    看起来"聚合生效了"，实际是把数据丢光了。
    """
    tr = at.ActivityTrace(generation_id="g_str")
    for _ in range(10):
        tr.record("text_delta", detail="这是一段正文")     # 裸字符串
    tr.flush()
    rows = at.drain_for_test()
    assert rows, "裸字符串形式的 text_delta 被完全丢弃"
    assert any("字" in r["brief"] for r in rows), f"没有统计到字数：{rows}"
    n = sum(int(re.search(r"\+(\d+)", r["brief"]).group(1))
            for r in rows if re.search(r"\+(\d+)", r["brief"]))
    assert n == len("这是一段正文") * 10, f"字数算错：{n}"


def test_sub_prefixed_streaming_detail_as_string():
    """子代理形式 `sub.text_delta` 同样要支持裸字符串。"""
    tr = at.ActivityTrace(generation_id="g_substr")
    for _ in range(5):
        tr.record("sub.text_delta", name="researcher", detail="abcdef")
    tr.flush()
    rows = at.drain_for_test()
    total = sum(int(re.search(r"\+(\d+)", r["brief"]).group(1))
                for r in rows if re.search(r"\+(\d+)", r["brief"]))
    assert total == 30, f"子代理裸字符串字数算错：{total}"


def test_aggregated_text_keeps_agent_attribution():
    """★ 聚合后的字数行必须带**哪个子代理**在写。

    ## 这是真机 4 子代理演示抓出来的缺口

    聚合按「类型」分桶（_pending_text / _pending_reason），但没有按 agent 分桶。
    于是 4 个子代理并行时，日志里全是：

        action=thinking brief=+450 字

    无法回答「这 450 字是 query_scale 还是 query_penetration 写的」——
    而那正是多代理场景下最需要的信息（谁在干活、谁卡住）。

    修法：聚合缓冲按 (kind, agent) 分桶，落盘时带上 agent。
    """
    tr = at.ActivityTrace(generation_id="g_attr")
    for _ in range(5):
        tr.record("sub.text_delta", name="query_scale", detail="abcdef")
    for _ in range(3):
        tr.record("sub.text_delta", name="query_penetration", detail="xyz")
    tr.flush()
    rows = at.drain_for_test()
    joined = " ".join(r["brief"] for r in rows)
    assert "query_scale" in joined, f"聚合行丢了 query_scale 归属：{rows}"
    assert "query_penetration" in joined, f"聚合行丢了 query_penetration 归属：{rows}"


def test_aggregated_buckets_are_per_agent_not_merged():
    """两个 agent 的字数不能合并成一条（否则看不出各自产出）。"""
    tr = at.ActivityTrace(generation_id="g_bucket")
    tr.record("sub.text_delta", name="a1", detail="x" * 100)
    tr.record("sub.text_delta", name="a2", detail="y" * 50)
    tr.flush()
    rows = at.drain_for_test()
    sizes = []
    for r in rows:
        m = re.search(r"\+(\d+)", r["brief"])
        if m:
            sizes.append(int(m.group(1)))
    assert sorted(sizes) == [50, 100], (
        f"两个 agent 的字数被合并成 {sizes}，应分别为 50 与 100")


def test_root_agent_text_still_has_label():
    """根代理自己的字数行也要能识别（不能与子代理混在一起）。"""
    tr = at.ActivityTrace(generation_id="g_rootfb")
    tr.record("text_delta", detail="z" * 30)
    tr.flush()
    rows = at.drain_for_test()
    assert rows
    assert "root" in rows[0]["brief"], f"根代理的字数行没有标识：{rows[0]}"


def test_sub_tool_events_keep_agent_name():
    """子代理的工具调用行必须能看出是哪个 agent 调的工具。"""
    tr = at.ActivityTrace(generation_id="g_toolattr")
    tr.record("sub.tool_start", name="query_scale",
              detail={"query": "metric_search"})
    rows = at.drain_for_test()
    assert rows
    assert "query_scale" in rows[0]["brief"], (
        f"工具调用行看不出是哪个 agent：{rows[0]}")


def test_thinking_action_name_is_stable():
    """★ 思考流的 action 名必须是 `thinking`，不能变成 `reasoning`。

    ## 这是真机跨轮对比暴露的日志契约破坏

    run1 的时间轴是 `action=thinking`，改了一版聚合后 run2 变成
    `action=reasoning`。后果：同一份核验脚本对同一篇文档算出两个数
    （思考字数 72,470 vs 0），看起来像文档写错了，实际是日志改名。

    日志是**跨轮对比**的依据，action 名必须稳定。
    """
    tr = at.ActivityTrace(generation_id="g_kind")
    tr.record("thinking_delta", detail="x" * 10)
    tr.record("sub.thinking_delta", name="a1", detail="y" * 10)
    tr.flush()
    rows = at.drain_for_test()
    actions = {r["action"] for r in rows}
    assert "thinking" in actions, f"思考流的 action 名不是 thinking：{actions}"
    assert "reasoning" not in actions, (
        f"出现了 reasoning —— 日志契约被破坏，跨轮对比会失效：{actions}")
