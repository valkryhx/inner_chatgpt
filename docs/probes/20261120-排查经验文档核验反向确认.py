"""反向确认：证明排查经验文档的核验脚本真的能抓到文档错误。

## 三个用例

0. **基线不改 → 必须全绿**
1. 把 writer 数量的期望值改错（4 → 5）→ 必须红
2. 把 owner 期望值改错 → 必须红
3. 把一条 SQL 的期望内容改成不存在的串 → 必须红

## 为什么这条必须有

`20261120-排查经验文档核验.py` 里全是「期望值 vs DB 实际」的比对。
如果我**把期望值写成和 DB 一致的错误值**，它会一直绿 —— 那是自证。

正确的检验是：**故意把期望值改错，看它会不会红**。
本轮真实收获：这条反向确认之前，核验脚本已经抓出我文档里
「5 个 writer」应为「4 个 writer」的错。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

PYTEST_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def _find_repo_root(start: pathlib.Path) -> pathlib.Path:
    for p in [start, *start.parents]:
        if (p / "backend" / "app" / "workspace.py").is_file():
            return p
    raise SystemExit(f"找不到仓库根（从 {start} 向上）")


REPO = _find_repo_root(pathlib.Path(__file__).resolve().parent)
PROBE = REPO / "docs" / "probes" / "20261120-排查经验文档核验.py"


def _run() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=REPO, capture_output=True, text=True, timeout=180,
        encoding="utf-8", errors="replace", env=PYTEST_ENV,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _inject_and_run(old: str, new: str) -> tuple[str, int, str]:
    orig = PROBE.read_text(encoding="utf-8")
    if old not in orig:
        return "inject_failed", -1, f"找不到注入片段：{old[:80]!r}"
    patched = orig.replace(old, new, 1)
    if patched == orig:
        return "inject_failed", -1, "replace 没有改变内容"
    try:
        PROBE.write_text(patched, encoding="utf-8", newline="\n")
        rc, out = _run()
        return "ran", rc, out
    finally:
        PROBE.write_text(orig, encoding="utf-8", newline="\n")


def main() -> int:
    problems: list[str] = []
    checked = 0

    rc, out = _run()
    if rc == 0:
        print("[✓ 基线] 未修改时全绿")
        checked += 1
    else:
        problems.append(f"基线应全绿，实际 rc={rc}\n{out[-600:]}")
        print("[✗ 基线] 没改就红了")

    # ---- 用例 1：writer 数量期望值改错 ----
    status, rc, out = _inject_and_run('"writer_count": 4,', '"writer_count": 5,')
    if status == "inject_failed":
        problems.append(f"用例1 注入失败：{out}")
        print(f"[✗ 用例1] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例1] writer 数量期望值改错 → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例1：数量改成 5 仍然绿 —— 数量断言无因果力")
        print("[✗ 用例1] 数量改错仍然绿")

    # ---- 用例 2：owner 期望值改错 ----
    status, rc, out = _inject_and_run(
        '"report_owner": "sub:writer_e",',
        '"report_owner": "sub:nobody_here",',
    )
    if status == "inject_failed":
        problems.append(f"用例2 注入失败：{out}")
        print(f"[✗ 用例2] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例2] owner 期望值改错 → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例2：owner 改成不存在的 agent 仍然绿 —— 断言无因果力")
        print("[✗ 用例2] owner 改错仍然绿")

    # ---- 用例 3：SQL 期望内容改成不存在的串 ----
    status, rc, out = _inject_and_run(
        '        "researcher_a",\n',
        '        "no_such_agent_xyz",\n',
    )
    if status == "inject_failed":
        problems.append(f"用例3 注入失败：{out}")
        print(f"[✗ 用例3] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例3] SQL 期望内容改成不存在 → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例3：期望内容不存在仍然绿 —— SQL 断言无因果力")
        print("[✗ 用例3] SQL 期望改错仍然绿")

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