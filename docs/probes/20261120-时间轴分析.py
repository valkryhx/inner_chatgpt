"""从 agent-activity 日志算出「时间花在哪」的定量分解。

## 回答什么问题

用户的原话：「撰写任务为什么写得这么慢，是他真的写那么多内容，
还是说响应的慢，还是别的原因？」

这三种原因在时间轴上长得完全不同：

| 原因 | 时间轴特征 |
| --- | --- |
| 真写了很多内容 | `text` 动作密集、字数大、**帧间隔短** |
| LLM 响应慢 | `thinking`/`text` 之间有大段静默，但每帧字数不多 |
| 工具慢 | `tool_start` → `tool_result` 间隔大 |
| 等待/空转 | 两帧之间静默很长，且前后都没有动作 |

所以判据不是「总时长」，而是**静默间隔分布 + 各动作的时间占比**。

## 用法

    python work/_analyze_trace.py <logfile> [pattern]
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict

# gen=g_xxx who=root t=57594ms action=sub.thinking_delta brief=...
LINE = re.compile(
    r"agent-activity gen=(?P<gen>\S+) who=(?P<who>\S+) t=(?P<t>\d+)ms "
    r"action=(?P<action>\S+) brief=(?P<brief>.*)$")


def parse(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            m = LINE.search(ln)
            if not m:
                continue
            d = m.groupdict()
            d["t"] = int(d["t"])
            rows.append(d)
    rows.sort(key=lambda r: r["t"])
    return rows


def words(brief: str) -> int:
    """从 `+N 字` 摘要里取字数。"""
    m = re.search(r"\+(\d+)", brief or "")
    return int(m.group(1)) if m else 0


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python work/_analyze_trace.py <logfile> [gen_id]")
        return 2
    rows = parse(sys.argv[1])
    want = sys.argv[2] if len(sys.argv) > 2 else ""
    if want:
        rows = [r for r in rows if r["gen"].startswith(want)]
    if not rows:
        print("没有解析到任何 agent-activity 行")
        return 1

    print(f"共 {len(rows)} 条，跨度 {rows[0]['t']/1000:.1f}s → {rows[-1]['t']/1000:.1f}s")
    span = (rows[-1]["t"] - rows[0]["t"]) / 1000.0 or 1.0

    # ---- 静默间隔：最大的那些 ----
    gaps = []
    for a, b in zip(rows, rows[1:]):
        gaps.append((b["t"] - a["t"], a, b))
    gaps.sort(key=lambda x: -x[0])

    print(f"\n=== 最大的 15 段静默（占跨度 {span:.0f}s 的比例）===")
    total_gap = sum(g[0] for g in gaps) / 1000.0
    silent = 0.0
    for ms, a, b in gaps[:15]:
        silent += ms / 1000.0
        print(f"  {ms/1000:7.1f}s  "
              f"上一动作={a['action']:<24s} 下一动作={b['action']:<24s} "
              f"@t={a['t']/1000:.0f}s")
        print(f"           上: {a['brief'][:60]}")
        print(f"           下: {b['brief'][:60]}")
    print(f"  前 15 段合计 {silent:.1f}s / 总间隔 {total_gap:.1f}s "
          f"（占 {silent/max(total_gap,1)*100:.0f}%）")

    # ---- 动作分布 ----
    by_action: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        by_action[r["action"]].append(r["t"])
    print("\n=== 动作分布 ===")
    for act, ts in sorted(by_action.items(), key=lambda kv: -len(kv[1])):
        print(f"  {act:<28s} {len(ts):5d} 次")

    # ---- 工具耗时 ----
    print("\n=== 工具调用耗时（tool_start → 同 id 的 tool_result）===")
    starts: dict[str, dict] = {}
    dur: list[tuple[float, str, str]] = []
    for r in rows:
        base = r["action"]
        if base.endswith("tool_start"):
            starts[r["brief"].split()[0] if r["brief"] else "?"] = r
        elif base.endswith("tool_result"):
            key = r["brief"].split()[0] if r["brief"] else "?"
            st = starts.pop(key, None)
            if st:
                dur.append(((r["t"] - st["t"]) / 1000.0, key, st["brief"][:50]))
    dur.sort(reverse=True)
    for sec, name, brief in dur[:12]:
        print(f"  {sec:7.1f}s  {name:<22s} {brief}")
    if dur:
        print(f"  合计 {sum(d[0] for d in dur):.1f}s / {len(dur)} 次调用")

    # ---- 文字产出 ----
    total_chars = sum(words(r["brief"]) for r in rows if r["action"].endswith(("text", "thinking")))
    n_text = sum(1 for r in rows if r["action"].endswith("text"))
    print(f"\n=== 文字产出 ===\n  累计 {total_chars} 字，"
          f"分 {n_text} 批落盘，平均 {total_chars/max(n_text,1):.0f} 字/批")
    print(f"  产出速率 {total_chars/span:.1f} 字/秒（跨度 {span:.0f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
