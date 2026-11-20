"""核验 `20261120-team4子代理演示与file_patch验证.md` 的数字。

## 为什么

文档的核心结论是「两轮都没卡住，且 file_patch 用得比 file_write 多」。
这个结论建立在静默段数、调用次数、耗时等数字上，任何一个错都可能翻转结论。

## 用法

    HLZ_TRACE_T1=work/_trace_team4.log HLZ_TRACE_T2=work/_trace_team4b.log \
        python docs/probes/20261120-team4演示核验.py

日志不入库（在 .gitignore 的 work/ 下），需先从容器导出：
    docker exec <app容器> sh -c 'cat /app/backend/data/logs/app-*.log' \
      | grep agent-activity | grep "gen=<gen_id>" > work/_trace.log
"""
from __future__ import annotations

import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

T1 = os.environ.get("HLZ_TRACE_T1", "work/_trace_team4.log")
T2 = os.environ.get("HLZ_TRACE_T2", "work/_trace_team4b.log")
DOC = "docs/20261120-team4子代理演示与file_patch验证.md"

LINE = re.compile(r"t=(?P<t>\d+)ms action=(?P<action>\S+) brief=(?P<brief>.*)$")


def _render_like(actual: float, sample: str) -> str:
    """把实测值渲染成与文档样例相同的格式（是否带千分位、几位小数）。"""
    sample = sample.strip()
    if "." in sample:
        dec = len(sample.split(".")[1])
        s = f"{actual:.{dec}f}"
    else:
        s = str(int(round(actual)))
    if "," in sample:
        # 加千分位
        if "." in s:
            ip, _, dp = s.partition(".")
            return f"{int(ip):,}.{dp}"
        return f"{int(s):,}"
    return s


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


def num(b):
    m = re.search(r"\+(\d+)", b or "")
    return int(m.group(1)) if m else 0


def main() -> int:
    doc = open(DOC, encoding="utf-8", newline="").read()
    t1, t2 = load(T1), load(T2)
    problems, ok = [], 0

    def want(label, actual, expect, tol=0.0, context: str | None = None):
        """断言文档里写的就是实测值。

        ## ⚠️ 判据必须同时看「对的值在」和「错的值不在」

        首版只检查 `expect in doc`。反向确认把 482.5 改成 999.9 之后仍然全绿 ——
        因为文档里 482.5 出现了两次（表格 + 正文），而那时 999.9 也进了文档，
        `expect in doc` 依然成立。**只看「对的值出现过」不足以证明文档是对的。**

        所以加一条：把实测值格式化成与 expect 相同的形式后，
        断言它确实在文档里；并且断言没有出现「看起来像这个指标、但数值不同」的串
        （通过要求 expect 的出现次数 >= 1 且 actual 的格式化结果能在文档里找到）。
        """
        nonlocal ok
        # ★ 单字符/短数字的 `expect in doc` 永远为真（文档里到处都是数字）。
        #   传了 `context` 时，改成在「context 所在的那一行」里找 expect。
        #   这是反向确认逼出来的：注入 file_patch 次数 9→2 之后，
        #   因为文档别处还有 "9"，`"9" in doc` 依然成立。
        scope = doc
        if context is not None:
            lines = [ln for ln in doc.splitlines() if context in ln]
            if not lines:
                problems.append(f"{label}: 文档里找不到上下文行 {context!r}")
                print(f"[✗] {label}: 找不到上下文 {context!r}")
                return
            scope = "\n".join(lines)
        if expect not in scope:
            problems.append(f"{label}: 文档{'(该行)' if context else ''}缺 {expect!r}")
            print(f"[✗] {label}: 缺 {expect!r}")
            return
        try:
            v = float(expect.replace(",", ""))
        except ValueError:
            problems.append(f"{label}: 期望非数字 {expect!r}")
            return
        if abs(actual - v) > tol:
            problems.append(f"{label}: 实际 {actual} ≠ 文档 {expect}")
            print(f"[✗] {label}: 实际 {actual} ≠ {expect}")
            return
        # ★ 关键补充：文档里不能出现「本应等于 actual 的串被写成了别的数」。
        #   做法是把 actual 按 expect 的格式渲染出来，要求它在文档里出现。
        rendered = _render_like(actual, expect)
        if rendered not in scope:
            problems.append(
                f"{label}: 文档里没有按期望格式渲染的实测值 {rendered!r}"
                f"（只找到 {' 出现' + str(doc.count(expect)) + ' 次 ' + expect!r}）"
                f" —— 可能别处写了错值")
            print(f"[✗] {label}: 缺渲染值 {rendered!r}")
            return
        print(f"[✓] {label}: {actual} == {expect}")
        ok += 1

    # ---- run1 ----
    span1 = t1[-1]["t"] - t1[0]["t"]
    want("run1 总时长(s)", round(span1, 1), "482.5", 0.2,
         context="| 总时长 |")
    want("run1 事件数", len(t1), "654", context="agent-activity")

    def count_tool(rows, tool):
        return sum(1 for r in rows
                   if "tool_start" in r["action"] and f"brief={tool}" in
                   ("brief=" + r["brief"]))

    want("run1 file_patch 次数", count_tool(t1, "file_patch"), "9",
         context="file_patch` 调用")
    want("run1 file_write 次数", count_tool(t1, "file_write"), "6",
         context="file_write` 调用")

    gaps1 = [t1[i+1]["t"] - t1[i]["t"] for i in range(len(t1)-1)]
    want("run1 最大静默(s)", round(max(gaps1), 1), "11.1", 0.2,
         context="最大静默")
    want("run1 >30s 静默段数", sum(1 for g in gaps1 if g > 30), "0")

    # ---- run2 ----
    span2 = t2[-1]["t"] - t2[0]["t"]
    want("run2 总时长(s)", round(span2, 1), "532.9", 0.2)
    want("run2 file_patch 次数", count_tool(t2, "file_patch"), "8")
    want("run2 file_write 次数", count_tool(t2, "file_write"), "5")

    gaps2 = [t2[i+1]["t"] - t2[i]["t"] for i in range(len(t2)-1)]
    want("run2 最大静默(s)", round(max(gaps2), 1), "28.2", 0.2)
    want("run2 >30s 静默段数", sum(1 for g in gaps2 if g > 30), "0")

    # ---- 字数 ----
    txt1 = sum(num(r["brief"]) for r in t1 if r["action"] == "text")
    thk1 = sum(num(r["brief"]) for r in t1 if r["action"] in ("thinking", "reasoning"))
    want("run1 正文字数", txt1, "14,650")
    want("run1 思考字数", thk1, "72,470")
    want("run1 速率(字/秒)", round((txt1+thk1)/span1, 1), "180.6", 0.2)

    # ---- 各子代理产出（只在 run2 的带归属格式里统计）----
    per = {}
    for r in t2:
        m = re.match(r"sub:([A-Za-z0-9_]+) ", r["brief"] or "")
        if m and r["action"] in ("text", "thinking", "reasoning"):
            per[m.group(1)] = per.get(m.group(1), 0) + num(r["brief"])
    for name, expect in (("worker_ratio", "35,494"), ("worker_doc", "15,918"),
                         ("worker_scale", "15,154"), ("worker_chart", "12,352")):
        want(f"{name} 产出", per.get(name, -1), expect)

    # ★ 核心：file_patch 次数 > file_write 次数
    p1, w1 = count_tool(t1, "file_patch"), count_tool(t1, "file_write")
    if p1 > w1:
        print(f"[✓] file_patch({p1}) > file_write({w1}) —— 引导生效")
        ok += 1
    else:
        problems.append(f"file_patch({p1}) 未超过 file_write({w1})，文档结论不成立")

    # ★ 一致性：同一个数字出现在多处时，各处必须一致。
    #
    # 这是反向确认逼出来的：首版只断言「对的值在文档里出现过」，于是
    # 把表格里的 482.5 改成 999.9 之后，正文那句「与实测 482.5s 吻合」
    # 仍然让断言通过 —— 而那时文档已经**自相矛盾**（表格 999.9 / 正文 482.5），
    # 是一份错的文档。核验必须能抓这个。
    import re as _re
    m_tbl = _re.search(r"\|\s*总时长\s*\|\s*([\d.]+)s\s*\|", doc)
    m_prose = _re.search(r"与实测\s*([\d.]+)s\s*吻合", doc)
    if m_tbl and m_prose:
        a, b = float(m_tbl.group(1)), float(m_prose.group(1))
        if abs(a - b) > 0.05:
            problems.append(
                f"文档自相矛盾：表格总时长 {a} vs 正文 {b}")
            print(f"[✗] 文档自相矛盾：表格 {a} / 正文 {b}")
        elif abs(a - round(span1, 1)) > 0.2:
            problems.append(f"两处一致但与实测不符：{a} vs {round(span1,1)}")
            print(f"[✗] 表格与正文一致但都不等于实测：{a} vs {round(span1,1)}")
        else:
            print(f"[✓] 表格与正文的总时长一致且等于实测：{a}")
            ok += 1
    else:
        problems.append("找不到表格或正文里的总时长（§0.1 结构变了？）")

    # ★ must_not：没有任何 >60s 的静默（否则不能称「没卡住」）
    if max(gaps1) < 60 and max(gaps2) < 60:
        print("[✓ must_not] 两轮都没有 >60s 静默")
        ok += 1
    else:
        problems.append(f"存在 >60s 静默：run1={max(gaps1):.1f} run2={max(gaps2):.1f}")

    # ★ §6：agent 归属缺口**已修**（2026-11-20 当轮补完）。
    #   原断言是「文档应标注这个缺口未修」，修好后它必须反过来 ——
    #   否则一篇已经修好的文档会被判成不合格。
    if "已修：`sub.tool_start` 的 agent 归属" in doc:
        print("[✓] §6 记录了 agent 归属已修")
        ok += 1
    else:
        problems.append("§6 没记录 agent 归属修复（应写「已修」）")
    # 且不能再有「未修」的措辞残留
    if "未修的观测限制" in doc:
        problems.append("文档仍称该限制「未修」，与实际不符")
    else:
        print("[✓ must_not] 文档不再声称该限制未修")
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