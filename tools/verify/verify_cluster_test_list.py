"""校验 run_cluster_tests.sh 里的测试清单没有漏（2026-11-19 审计 §5）。

## 为什么需要

`run_cluster_tests.sh` 里硬编码了「需要真实 Redis/PG 的测试文件」清单。
新增一个这样的测试文件时，很容易忘记加进去 —— 于是它永远被跳过，
而没人知道。

这个脚本扫 `tests/*.py`，找出**引用了** `HLZ_TEST_REDIS_URL` 或
`HLZ_TEST_PG_DSN` 的文件，与 shell 脚本里的清单比对。少了就报出来。

## 用法

    python tools/verify/verify_cluster_test_list.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1] / "backend"
TESTS = BACKEND / "tests"
SHELL = TESTS / "manual" / "run_cluster_tests.sh"

MARKERS = ("HLZ_TEST_REDIS_URL", "HLZ_TEST_PG_DSN")

# 明确**不**进 shell 清单的（由脚本单独跑，或不需要真实依赖）。
EXEMPT: dict[str, str] = {
    # 这几个在 shell 脚本里分开跑（PG 那组），但为了清单完整也一并列出
}


def files_needing_deps() -> set[str]:
    out: set[str] = set()
    for p in sorted(TESTS.glob("test_*.py")):
        text = p.read_text(encoding="utf-8", errors="replace")
        if any(m in text for m in MARKERS):
            out.add(p.name)
    return out


def files_in_shell() -> set[str]:
    if not SHELL.exists():
        return set()
    text = SHELL.read_text(encoding="utf-8", errors="replace")
    # 抓 `tests/xxx.py` 形态的引用
    return {Path(m).name for m in re.findall(r"tests/([\w./-]+\.py)", text)}


def main() -> int:
    needed = files_needing_deps()
    listed = files_in_shell()

    missing = needed - listed - set(EXEMPT)
    extra = listed - needed

    print(f"需要真实依赖的测试文件：{len(needed)} 个")
    for n in sorted(needed):
        mark = "ok" if n in listed else "缺"
        print(f"  [{mark}] {n}")

    bad = False
    if missing:
        bad = True
        print("\n!! 这些文件需要真实依赖，但 run_cluster_tests.sh 里没有：")
        for n in sorted(missing):
            print(f"     tests/{n}")
        print("   加进去，否则它们永远被静默跳过。")

    if extra:
        # 不算错：shell 里可能列了 sqlcompat 这种不需要标记但也要跑 PG 的
        print("\n注意：shell 里列了但它不引用依赖标记（可能是故意的）：")
        for n in sorted(extra):
            print(f"     tests/{n}")

    if not SHELL.exists():
        print(f"\n!! 找不到 {SHELL}")
        return 2

    if bad:
        return 1
    print("\n清单一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())