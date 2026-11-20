"""反向确认：故意注入错数字，核验脚本必须变红。

## 为什么必须做（AGENTS.md §8.6）

上一轮核验脚本抓出 3 处，其中 2 处是**脚本自己的判据错**
（`507` 命中 id、静默后首条是容器事件）。这说明脚本本身也需被验证。

## 两个用例

1. 注入错数字 → 必须 FAIL（证明脚本会红）
2. 不改动 → 必须 PASS（证明红的不是环境问题）
"""
from __future__ import annotations

import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DOC = "docs/20261120-多智能体读写慢的定量排查.md"
SCRIPT = "work/_verify_multiaagent_doc.py"


def run() -> tuple[int, str]:
    r = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    orig = open(DOC, encoding="utf-8", newline="").read()

    # 用例 1：应该 PASS
    rc, out = run()
    if rc != 0:
        print("✗ 基线不通过，反向确认无意义：")
        print(out[-800:])
        return 1
    print("[✓] 用例 1（基线）：PASS")

    # 用例 2：注入错数字，应该 FAIL
    bad = orig.replace("58,385", "99,999")
    if bad == orig:
        print("✗ 注入失败——字符串没匹配上，无法证明脚本会变红")
        return 1
    open(DOC, "w", encoding="utf-8", newline="\n").write(bad)
    try:
        rc2, out2 = run()
    finally:
        open(DOC, "w", encoding="utf-8", newline="\n").write(orig)

    if rc2 == 0:
        print("✗ 注入了错数字脚本仍然全绿 ⇒ 核验无效")
        return 1
    print("[✓] 用例 2（注入 99,999）：FAIL（符合预期）")

    # 确认已恢复
    rc3, _ = run()
    if rc3 != 0:
        print("✗ 恢复后仍失败，文档可能处于半改状态")
        return 1
    print("[✓] 已恢复，基线重新 PASS")
    print("\n✓ 反向确认通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
