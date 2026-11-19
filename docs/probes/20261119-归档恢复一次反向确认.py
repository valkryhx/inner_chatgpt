"""反向确认：恢复回滚 bug 的守卫被拆掉时，测试必须变红。

## 为什么这条不可省

这是**用户数据的静默损坏**：用户写的文件被旧归档覆盖，不报错、不告警。
而 bug 的成因恰恰是「docstring 写着只做一次、代码里没有守卫」——
一个只测「新建槽会恢复」的用例集会全绿。

所以必须证明：拆掉守卫后，那条测「第二次不许恢复」的用例真的会红。

## 两种破坏

1. **拆掉 `archived_restored` 守卫** → 每次 exec 都解包（原始 bug）
2. **把标记置位挪到解包之后** → 解包失败会反复重试，且部分解包会和归档叠加

跑法：
    python docs/probes/20261119-归档恢复一次反向确认.py
期望 `ok: 2/2`。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "backend" / "sandbox_service" / "main.py").exists():
            return p
    raise SystemExit(f"找不到仓库根（从 {start} 往上）")


ROOT = find_repo_root(Path(__file__).resolve())
BACKEND = ROOT / "backend"
MAIN = BACKEND / "sandbox_service" / "main.py"
TESTFILE = "tests/test_sandbox_service_restore_once.py"

GUARD = """    if getattr(slot, "archived_restored", False):
        return
    # 先置位再解包"""

CASES = [
    (
        "拆掉一次性守卫",
        GUARD,
        "    # 破坏：没有守卫\n    # 先置位再解包",
        ["test_second_exec_does_not_restore_again"],
    ),
    (
        "标记置位挪到解包之后（失败会重试）",
        GUARD,
        "    restored_now = True\n    # 先置位再解包",
        ["test_second_exec_does_not_restore_again"],
    ),
    (
        "归档回调里重置标记",
        "    try:\n        if not slot.workspace.exists():\n            return",
        "    slot.archived_restored = False   # 破坏：归档时重置\n"
        "    try:\n        if not slot.workspace.exists():\n            return",
        ["test_archive_does_not_reset_restore_flag"],
    ),
]


def run_tests() -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", TESTFILE, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=BACKEND, capture_output=True,
    )
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def failed_names(output: str) -> set[str]:
    names = set()
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("FAILED "):
            head = line[len("FAILED "):].split(" - ")[0]
            if "::" in head:
                names.add(head.split("::")[-1])
    return names


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ok = 0
    for name, original, broken, expect_red in CASES:
        text = MAIN.read_text(encoding="utf-8")
        if original not in text:
            print(f"[{name}] 跳过：main.py 里找不到目标片段（代码已变，请更新脚本）")
            continue
        try:
            MAIN.write_text(text.replace(original, broken, 1), encoding="utf-8")
            _rc, out = run_tests()
        finally:
            MAIN.write_text(text, encoding="utf-8")

        red = failed_names(out)
        missing = [t for t in expect_red if t not in red]
        if missing:
            print(f"[{name}] ✗ 期望变红 {expect_red}，实际红 {sorted(red)}")
            print(out[-1200:])
            continue
        print(f"[{name}] ✓ 按预期变红：{sorted(red & set(expect_red))}")
        ok += 1

    print(f"ok: {ok}/{len(CASES)}")
    return 0 if ok == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())