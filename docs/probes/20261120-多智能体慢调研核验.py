"""核验 `docs/20261120-多智能体读写慢的定量排查.md` 里的每个数字。

## 为什么

那份文档的核心结论是「慢在生成字数，不是多代理开销」。这个结论
完全建立在几个数字上（227 字符/秒、58,432 字符、53.1 秒静默、
wait_agent 279 秒）。任何一个数字错了，结论就翻转。

所以数字必须由脚本从原始日志复算，而不是我手抄。

## 判据

- 每个数字都从 `work/_trace_full.log` 重新算一遍
- 算出来与文档写的不一致 ⇒ 失败
- 同时断言「这些数字确实能区分两种状态」：例如 53.1s 的静默
  必须真的存在，不能容忍 0
"""
from __future__ import annotations

import os
import re
import sys

# Windows 控制台默认 GBK，直接 print 中文/✓ 会 UnicodeEncodeError。
# 按 AGENTS.md 的规矩：中文输出走字节流，不依赖控制台代码页。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

LOG = os.environ.get("HLZ_TRACE_LOG", "work/_trace_full.log")
DOC = "docs/20261120-多智能体读写慢的定量排查.md"

LINE = re.compile(
    r"t=(?P<t>\d+)ms action=(?P<action>\S+) brief=(?P<brief>.*)$")


def load():
    rows = []
    for ln in open(LOG, encoding="utf-8", errors="replace"):
        m = LINE.search(ln)
        if not m:
            continue
        d = m.groupdict()
        d["t"] = int(d["t"]) / 1000.0
        rows.append(d)
    rows.sort(key=lambda r: r["t"])
    return rows


def brief_chars(b: str) -> int:
    """子代理 brief 形如 `sub:researcher_t1 正文`，取正文长度。"""
    if b.startswith("sub:"):
        parts = b.split(" ", 1)
        return len(parts[1]) if len(parts) > 1 else 0
    return len(b)


def main() -> int:
    rows = load()
    doc = open(DOC, encoding="utf-8").read()
    problems = []
    ok = 0

    def want(label, actual, expect_in_doc, tol=0):
        nonlocal ok
        found = expect_in_doc in doc
        match = abs(actual - float(expect_in_doc.replace(",", ""))) <= tol
        if not found:
            problems.append(f"{label}: 文档里找不到 {expect_in_doc!r}")
            print(f"[✗] {label}: 文档缺 {expect_in_doc!r}")
            return
        if not match:
            problems.append(f"{label}: 实际 {actual}，文档写 {expect_in_doc}")
            print(f"[✗] {label}: 实际 {actual} ≠ 文档 {expect_in_doc}")
            return
        print(f"[✓] {label}: {actual} == {expect_in_doc}")
        ok += 1

    # 1) 事件总数
    want("时间轴事件数", len(rows), "15,377", tol=0)

    # 2) 流式帧数
    deltas = [r for r in rows if "delta" in r["action"]]
    want("流式帧数", len(deltas), "7,564", tol=0)

    # 3) 总跨度
    span = rows[-1]["t"] - rows[0]["t"]
    want("总跨度(s)", round(span, 1), "359.3", tol=0.15)

    # 4) 累计字符
    total_chars = sum(brief_chars(r["brief"]) for r in deltas)
    want("累计字符", total_chars, "58,385", tol=0)

    # 5) 最大静默
    gaps = [(rows[i + 1]["t"] - rows[i]["t"], rows[i], rows[i + 1])
            for i in range(len(rows) - 1)]
    maxg = max(gaps, key=lambda x: x[0])
    want("最大静默(s)", round(maxg[0], 1), "53.1", tol=0.15)

    # ★ 静默边界：前一条必须是 text_delta，后一条必须是 file_write
    #   这是「写在生成而不是写盘」的核心证据
    if "text_delta" not in maxg[1]["action"]:
        problems.append(f"最大静默的前一条不是 text_delta：{maxg[1]['action']}")
    else:
        print(f"[✓] 最大静默前一条是 text_delta")
        ok += 1
    # 静默后的第一条往往是容器事件 `subagent_event`（空 brief），
    # 真正有语义的是它后面那条 —— 跳过容器再看。
    after = maxg[2]
    idx = rows.index(after)
    while idx < len(rows) - 1 and rows[idx]["action"] == "subagent_event":
        idx += 1
        after = rows[idx]
    if "file_write" not in after["brief"]:
        problems.append(f"最大静默后首个有语义的动作不是 file_write：{after['action']} {after['brief'][:40]}")
    else:
        print(f"[✓] 最大静默后首个有语义的动作是 file_write")
        ok += 1

    # 6) wait_agent 合计
    wait = 0.0
    t0s = {}
    for r in rows:
        if r["action"] == "tool_start":
            nm = (r["brief"].split() or ["?"])[0]
            if nm.startswith("wait_agent"):
                t0s[nm] = r["t"]
        elif r["action"] == "tool_result":
            nm = (r["brief"].split() or ["?"])[0]
            t = t0s.pop(nm, None)
            if t is not None:
                wait += r["t"] - t
    want("wait_agent 合计(s)", round(wait, 1), "279.0", tol=0.15)

    # 7) 吐字速率
    active = sum(b["t"] - a["t"] for a, b in zip(rows, rows[1:])
                 if b["t"] - a["t"] <= 10)
    rate = total_chars / active
    want("吐字速率(字符/秒)", round(rate, 1), "227", tol=1.0)

    # 8) 否定断言：没有 507 / PermissionError
    text = open(LOG, encoding="utf-8", errors="replace").read()
    # ⚠️ 不能用裸 "507"：本次 generation id 是 `g_a8507cfa25`，
    # 里面就含 "507"（与上次 `50742` 端口号同一类假阳性）。
    # 必须用能区分「HTTP 507 状态」与「id 里的字符」的模式。
    bad = re.findall(r'HTTP/1\.1"\s+507|PermissionError|status[=: ]+507', text)
    if bad:
        problems.append(f"日志里出现真实 507/权限错 {len(bad)} 次，与文档矛盾")
    else:
        print("[✓ must_not] 无真实 HTTP 507 / PermissionError（裸字符串命中是 id 字符）")
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
