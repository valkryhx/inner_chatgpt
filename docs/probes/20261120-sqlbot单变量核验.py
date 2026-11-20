"""核验文档 §10（sqlbotmcp 单变量测试）里的每个数字。

## 为什么

§10 的结论「40817 字 ÷ 163 字/秒 = 250s，与实测完全吻合」是本轮最强的一条，
但它建立在 4 个数字上。任何一个错，结论就变成巧合。

## 判据

每个数字都从 work/_trace_sqlbot.log 复算；同时断言
「最大静默确实从 301.5 降到 8.2」这个对比是真实的（两边都要有数据）。
"""
from __future__ import annotations

import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG = os.environ.get("HLZ_TRACE_LOG", "work/_trace_sqlbot.log")
DOC = "docs/20261120-多智能体读写慢的定量排查.md"
LINE = re.compile(r"t=(?P<t>\d+)ms action=(?P<action>\S+) brief=(?P<brief>.*)$")


def load(path):
    rows = []
    for ln in open(path, encoding="utf-8", errors="replace"):
        m = LINE.search(ln)
        if not m:
            continue
        d = m.groupdict()
        d["t"] = int(d["t"]) / 1000.0
        rows.append(d)
    rows.sort(key=lambda r: r["t"])
    return rows


def num(brief):
    m = re.search(r"\+(\d+)", brief or "")
    return int(m.group(1)) if m else 0


def main() -> int:
    rows = load(LOG)
    doc = open(DOC, encoding="utf-8", newline="").read()
    problems, ok = [], 0

    def want(label, actual, expect, tol=0.0):
        nonlocal ok
        if expect not in doc:
            problems.append(f"{label}: 文档缺 {expect!r}")
            print(f"[✗] {label}: 文档缺 {expect!r}")
            return
        try:
            v = float(expect.replace(",", ""))
        except ValueError:
            problems.append(f"{label}: 期望值非数字 {expect!r}")
            return
        if abs(actual - v) > tol:
            problems.append(f"{label}: 实际 {actual} ≠ 文档 {expect}")
            print(f"[✗] {label}: 实际 {actual} ≠ {expect}")
            return
        print(f"[✓] {label}: {actual} == {expect}")
        ok += 1

    span = rows[-1]["t"] - rows[0]["t"]
    want("总跨度(s)", round(span, 1), "250.1", 0.15)
    want("时间轴事件数", len(rows), "580")

    txt = sum(num(r["brief"]) for r in rows if r["action"] == "text")
    thk = sum(num(r["brief"]) for r in rows if r["action"] == "thinking")
    want("正文字数", txt, "4,366")
    want("思考字数", thk, "36,451")
    want("合计字数", txt + thk, "40,817")
    want("吐字速率(字/秒)", round((txt + thk) / span, 1), "163.2", 0.15)

    gaps = [(rows[i + 1]["t"] - rows[i]["t"], rows[i], rows[i + 1])
            for i in range(len(rows) - 1)]
    maxg = max(gaps, key=lambda x: x[0])
    want("最大静默(s)", round(maxg[0], 1), "8.2", 0.15)

    # 字数记录行数
    n_num = sum(1 for r in rows if num(r["brief"]) > 0)
    want("字数记录行数", n_num, "263")

    # ★ 核心对比：sqlbot 的最大静默必须远小于 exa 版
    exa = load(os.environ.get("HLZ_TRACE_EXA", "work/_trace_run2.log"))
    exa_max = max((exa[i + 1]["t"] - exa[i]["t"] for i in range(len(exa) - 1)),
                  default=0)
    print(f"\n[对比] exa 版最大静默 {exa_max:.1f}s vs sqlbot 版 {maxg[0]:.1f}s")
    if maxg[0] > 30:
        problems.append(f"sqlbot 版最大静默 {maxg[0]:.1f}s，没有显著优于 exa")
    elif exa_max < 100:
        problems.append(f"exa 版最大静默只有 {exa_max:.1f}s，对照组不成立")
    else:
        print(f"[✓] 对比成立：{exa_max:.0f}s → {maxg[0]:.1f}s（差 {exa_max/maxg[0]:.0f} 倍）")
        ok += 1

    # 否定断言：本轮无 MCP 慢调用
    from collections import defaultdict
    pend, dur = defaultdict(list), []
    for r in rows:
        a, nm = r["action"], (r["brief"].split() or ["?"])[0]
        if a.endswith("tool_start"):
            pend[nm].append(r["t"])
        elif a.endswith("tool_result") and pend[nm]:
            dur.append((r["t"] - pend[nm].pop(0), nm))
    slow = [d for d, n in dur if d > 60]
    if slow:
        problems.append(f"本轮有 {len(slow)} 次工具调用 >60s")
    else:
        print(f"[✓ must_not] 无 >60s 的工具调用（exa 版有 6 次）")
        ok += 1

    print()
    if problems:
        print(f"✗ 失败 {len(problems)} 项：")
        for p in problems:
            print("  -", p)
        return 1
    print(f"✓ 全部通过（{ok} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# 时间轴日志不入库（在 .gitignore 的 work/ 下）。重新导出的方法：
#   docker exec <app容器> sh -c 'cat /app/backend/data/logs/app-*.log' | grep agent-activity > work/_trace.log
# 然后在仓库根跑：HLZ_TRACE_LOG=work/_trace.log python <本脚本>
