"""反向确认：给 team4 核验脚本注入错数字，必须变红。"""
from __future__ import annotations

import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DOC = "docs/20261120-team4子代理演示与file_patch验证.md"
SCRIPT = "docs/probes/20261120-team4演示核验.py"


def run() -> int:
    r = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode


def main() -> int:
    orig = open(DOC, encoding="utf-8", newline="").read()

    if run() != 0:
        print("✗ 基线不通过")
        return 1
    print("[✓] 用例 1（基线）：PASS")

    # ⚠️ 注入串必须**全局唯一**。首版用 "482.5"，而文档里出现两次
    # （表格 + 正文），replace(count=1) 只改了第一处，第二处还在，
    # 于是核验照常通过 —— 反向确认自己踩了「假绿」。
    # 用带上下文的唯一串。
    cases = [
        ("| 总时长 | 482.5s | 532.9s |", "| 总时长 | 999.9s | 532.9s |", "错总时长"),
    ]
    injected = 0
    for old, new, label in cases:
        if label is None:
            continue
        bad = orig.replace(old, new)
        if bad == orig:
            print(f"✗ [{label}] 注入串没匹配上")
            return 1
        try:
            open(DOC, "w", encoding="utf-8", newline="\n").write(bad)
            rc = run()
        finally:
            open(DOC, "w", encoding="utf-8", newline="\n").write(orig)
        if rc == 0:
            print(f"✗ [{label}] 注入后仍全绿 ⇒ 核验无效")
            return 1
        print(f"[✓] [{label}] 注入后 FAIL")
        injected += 1

    # 再测一个：把「file_patch > file_write」的结论改反
    bad2 = orig.replace("`file_patch` 调用 | **9 次** | **8 次**",
                       "`file_patch` 调用 | **2 次** | **8 次**", 1)
    if bad2 == orig:
        print("[!] 未找到 file_patch 次数行，跳过该用例")
    else:
        try:
            open(DOC, "w", encoding="utf-8", newline="\n").write(bad2)
            rc = run()
        finally:
            open(DOC, "w", encoding="utf-8", newline="\n").write(orig)
        if rc == 0:
            print("✗ [错调用次数] 注入后仍全绿")
            return 1
        print("[✓] [错调用次数] 注入后 FAIL")
        injected += 1

    if run() != 0:
        print("✗ 恢复后仍失败")
        return 1
    print("[✓] 恢复后重新 PASS")
    print(f"\n✓ 反向确认通过（{injected} 个注入用例 + 基线）")
    return 0


if __name__ == "__main__":
    sys.exit(main())