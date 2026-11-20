"""分析 4 子代理这轮的时间轴：是否卡住、是否用了 file_patch。"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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


def num(b):
    m = re.search(r"\+(\d+)", b or "")
    return int(m.group(1)) if m else 0


def main() -> int:
    rows = load(sys.argv[1] if len(sys.argv) > 1 else "work/_trace_team4.log")
    span = rows[-1]["t"] - rows[0]["t"]
    print(f"跨度 {span:.1f}s，事件 {len(rows)}\n")

    txt = sum(num(r["brief"]) for r in rows if r["action"] == "text")
    thk = sum(num(r["brief"]) for r in rows if r["action"] == "thinking")
    print(f"=== 产出 ===\n  正文 {txt} 字，思考 {thk} 字，合计 {txt+thk} 字")
    print(f"  速率 {(txt+thk)/span:.1f} 字/秒")

    # 工具耗时
    pend = defaultdict(list)
    dur = []
    for r in rows:
        a, nm = r["action"], (r["brief"].split() or ["?"])[0]
        if a.endswith("tool_start"):
            pend[nm].append(r["t"])
        elif a.endswith("tool_result") and pend[nm]:
            dur.append((r["t"] - pend[nm].pop(0), nm))
    dur.sort(reverse=True)
    print(f"\n=== 工具耗时 top10（合计 {sum(d for d,_ in dur):.1f}s / {len(dur)} 次）===")
    for d, n in dur[:10]:
        print(f"  {d:8.1f}s  {n}")

    # 关键：file_patch vs file_write 的耗时对比
    patch = [d for d, n in dur if n == "file_patch"]
    write = [d for d, n in dur if n == "file_write"]
    print(f"\n=== file_patch vs file_write ===")
    if patch:
        print(f"  file_patch {len(patch)} 次，合计 {sum(patch):.2f}s，"
              f"平均 {sum(patch)/len(patch)*1000:.0f}ms")
    if write:
        print(f"  file_write {len(write)} 次，合计 {sum(write):.2f}s，"
              f"平均 {sum(write)/len(write)*1000:.0f}ms")

    # 静默
    gaps = sorted(((rows[i+1]["t"] - rows[i]["t"], rows[i], rows[i+1])
                   for i in range(len(rows)-1)), key=lambda x: -x[0])
    print(f"\n=== 最大静默 top8 ===")
    for g, a, b in gaps[:8]:
        print(f"  {g:7.1f}s @t={a['t']:7.0f}s {a['action']:<22s} → {b['action']}")

    big = [g for g, _, _ in gaps if g > 30]
    print(f"\n  >30s 静默：{len(big)} 段，合计 {sum(big):.1f}s"
          f"（占跨度 {sum(big)/span*100:.0f}%）")

    # 并发验证：两个查询子代理的活跃窗口是否重叠
    per = defaultdict(lambda: {"t0": None, "t1": None, "n": 0})
    for r in rows:
        m = re.match(r"sub:([A-Za-z0-9_]+)", r["brief"])
        if not m:
            continue
        d = per[m.group(1)]
        if d["t0"] is None:
            d["t0"] = r["t"]
        d["t1"] = r["t"]
        d["n"] += 1
    print("\n=== 各子代理活跃窗口（验并行/串行）===")
    for name, d in sorted(per.items()):
        if d["t0"] is None:
            continue
        print(f"  {name:<20s} {d['t0']:7.0f}s → {d['t1']:7.0f}s"
              f"（{d['t1']-d['t0']:6.1f}s，{d['n']} 事件）")

    # 重叠判定
    q = {k: v for k, v in per.items()
         if k.startswith("query_") and v["t0"] is not None}
    if len(q) >= 2:
        ks = sorted(q.items())
        a, b = ks[0][1], ks[1][1]
        overlap = min(a["t1"], b["t1"]) - max(a["t0"], b["t0"])
        print(f"\n  ★ 两个查询的活跃窗口重叠 {overlap:.1f}s "
              f"⇒ {'并行' if overlap > 5 else '串行！'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())