"""反向确认：证明「对齐 CC 的 Read」三项行为真的被测到。

## 四个用例

0. **基线不改 → 必须全绿**（没有这条，「全红」不携带信息）
1. `DEFAULT_READ_LINES` 改回 200 → 1500 行一次读完的测试必须红
2. 横幅里删掉 `start=` 参数 → 「横幅给出下一页参数」的测试必须红
3. 横幅里删掉「不要只看这一页」告警 → 对应测试必须红

## 为什么第 2、3 条要分开

它们是横幅的两个**独立作用**：① 降低推理负担（给参数）；
② 防止误判（给告警）。合成一条会掩盖其中一个失效。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

PYTEST_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}
TARGET = "tests/test_read_aligns_cc.py"


def _find_repo_root(start: pathlib.Path) -> pathlib.Path:
    for p in [start, *start.parents]:
        if (p / "backend" / "app" / "workspace.py").is_file():
            return p
    raise SystemExit(f"找不到仓库根（从 {start} 向上）")


REPO = _find_repo_root(pathlib.Path(__file__).resolve().parent)
BT = REPO / "backend" / "app" / "builtin_tools.py"


def _pytest(tests: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q", "--no-header", "-p", "no:randomly"],
        cwd=REPO / "backend", capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace", env=PYTEST_ENV,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _inject_and_run(old: str, new: str, tests: list[str]) -> tuple[str, int, str]:
    orig = BT.read_text(encoding="utf-8")
    if old not in orig:
        return "inject_failed", -1, f"找不到注入片段：{old[:80]!r}"
    patched = orig.replace(old, new, 1)
    if patched == orig:
        return "inject_failed", -1, "replace 没有改变内容"
    try:
        BT.write_text(patched, encoding="utf-8", newline="\n")
        rc, out = _pytest(tests)
        return "ran", rc, out
    finally:
        BT.write_text(orig, encoding="utf-8", newline="\n")


def main() -> int:
    problems: list[str] = []
    checked = 0

    # ---- 基线 ----
    rc, out = _pytest([TARGET])
    if rc == 0:
        print("[✓ 基线] 未修改时全绿")
        checked += 1
    else:
        problems.append(f"基线应全绿，实际 rc={rc}\n{out[-600:]}")
        print("[✗ 基线] 没改就红了")

    # ---- 用例 1：默认值改回 200 ----
    status, rc, out = _inject_and_run(
        "DEFAULT_READ_LINES = 2000",
        "DEFAULT_READ_LINES = 200",
        [f"{TARGET}::test_reads_1500_lines_in_one_call",
         f"{TARGET}::test_default_read_lines_is_2000"],
    )
    if status == "inject_failed":
        problems.append(f"用例1 注入失败：{out}")
        print(f"[✗ 用例1] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例1] 默认值改回 200 → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例1：改回 200 仍然绿 —— 默认值没有真被测到")
        print("[✗ 用例1] 改回 200 仍然绿")

    # ---- 用例 2：横幅删掉 start= 参数 ----
    status, rc, out = _inject_and_run(
        '''f"下一页：file_read path={shown!r} start={nxt} count={end - start + 1}\\n"''',
        '''f"下一页：继续读\\n"''',
        [f"{TARGET}::test_truncated_banner_says_how_to_page"],
    )
    if status == "inject_failed":
        problems.append(f"用例2 注入失败：{out}")
        print(f"[✗ 用例2] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例2] 横幅不给参数 → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例2：横幅不给参数仍然绿 —— 参数断言无因果力")
        print("[✗ 用例2] 横幅不给参数仍然绿")

    # ---- 用例 3：横幅删掉告警 ----
    status, rc, out = _inject_and_run(
        '''f"不要在只看这一页的情况下下结论 —— 答案可能在未显示的部分。]"''',
        '''f"]"''',
        [f"{TARGET}::test_truncated_banner_warns_against_answering_from_one_page"],
    )
    if status == "inject_failed":
        problems.append(f"用例3 注入失败：{out}")
        print(f"[✗ 用例3] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例3] 横幅缺告警 → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例3：横幅缺告警仍然绿 —— 告警断言无因果力")
        print("[✗ 用例3] 横幅缺告警仍然绿")

    # ---- 用例 4：不该有横幅时却有（over-trigger）----
    status, rc, out = _inject_and_run(
        "    if end >= total:\n        return \"\"",
        "    if False:\n        return \"\"",
        [f"{TARGET}::test_no_banner_when_whole_file_fits"],
    )
    if status == "inject_failed":
        problems.append(f"用例4 注入失败：{out}")
        print(f"[✗ 用例4] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例4] 读完了还给横幅 → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例4：读完了仍给横幅却全绿 —— 「不该误报」没有被测到")
        print("[✗ 用例4] 读完了仍给横幅却仍然绿")

    print()
    if problems:
        print(f"✗ 反向确认失败 {len(problems)} 项：")
        for p in problems:
            print("  -", p)
        return 1
    print(f"✓ 反向确认全部符合预期（{checked} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())