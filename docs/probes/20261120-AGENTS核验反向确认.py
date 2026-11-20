"""反向确认：AGENTS.md 核验脚本真的能抓到文档错误。

## 四个用例

0. **基线不改 → 必须全绿**
1. 把文档里某条 SQL 的字段名改错（`turns` → `no_such_col`）→ 必须红
2. 在文档里加回 `§8.5` 悬空引用 → 必须红
3. 把必读清单里那条路径改错 → 必须红

## 为什么要这条

核验脚本本身可能静默失效 —— 本轮实际踩到过：
第一版用 `startswith("select")` 判断，而文档 SQL 前面都有 `--` 注释行，
**三条全被跳过**却报「全部通过」。加了 `doc_sql_count == 0` 的兜底断言才算堵住。

反向确认是第二道保险：证明「改错文档」时脚本会变红。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def _find_repo_root(start: pathlib.Path) -> pathlib.Path:
    for p in [start, *start.parents]:
        if (p / "backend" / "app" / "workspace.py").is_file():
            return p
    raise SystemExit(f"找不到仓库根（从 {start} 向上）")


REPO = _find_repo_root(pathlib.Path(__file__).resolve().parent)
PROBE = REPO / "docs" / "probes" / "20261120-AGENTS新增章节核验.py"
AGENTS = REPO / "AGENTS.md"


def _run() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=REPO, capture_output=True, text=True, timeout=180,
        encoding="utf-8", errors="replace", env=ENV,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _inject_and_run(old: str, new: str) -> tuple[str, int, str]:
    orig = AGENTS.read_text(encoding="utf-8")
    if old not in orig:
        return "inject_failed", -1, f"找不到注入片段：{old[:80]!r}"
    patched = orig.replace(old, new, 1)
    if patched == orig:
        return "inject_failed", -1, "replace 没有改变内容"
    try:
        AGENTS.write_text(patched, encoding="utf-8", newline="\n")
        rc, out = _run()
        return "ran", rc, out
    finally:
        AGENTS.write_text(orig, encoding="utf-8", newline="\n")


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

    # ---- 用例 1：文档 SQL 字段名改错 ----
    status, rc, out = _inject_and_run(
        "SELECT name, status, turns, (finished_at-started_at)/1000 AS secs",
        "SELECT name, status, no_such_col, (finished_at-started_at)/1000 AS secs",
    )
    if status == "inject_failed":
        problems.append(f"用例1 注入失败：{out}")
        print(f"[✗ 用例1] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例1] 文档 SQL 字段名错 → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例1：字段名改错仍然绿 —— SQL 核验无因果力")
        print("[✗ 用例1] 字段名改错仍然绿")

    # ---- 用例 2：加回 §8.5 悬空引用 ----
    status, rc, out = _inject_and_run(
        "### 2.6 与 §4 TDD 的衔接",
        "### 2.6 与 §8.5 TDD 的衔接",
    )
    if status == "inject_failed":
        problems.append(f"用例2 注入失败：{out}")
        print(f"[✗ 用例2] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例2] 加回 §8.5 悬空引用 → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例2：加回 §8.5 仍然绿 —— 悬空引用检查无因果力")
        print("[✗ 用例2] 加回 §8.5 仍然绿")

    # ---- 用例 3：必读清单路径改错 ----
    status, rc, out = _inject_and_run(
        "| `docs/20261120-排查经验-从卡住到根因的四路交叉.md` |",
        "| `docs/20261120-不存在的文档.md` |",
    )
    if status == "inject_failed":
        problems.append(f"用例3 注入失败：{out}")
        print(f"[✗ 用例3] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例3] 必读清单路径错 → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例3：路径改错仍然绿 —— 清单校验无因果力")
        print("[✗ 用例3] 路径改错仍然绿")

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