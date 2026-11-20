"""反向确认：umask 测试真的能抓到缺失。

## 三个用例

0. **不改任何东西 → 必须全绿**（没有这条，「全红」不携带信息）
1. 去掉 daemon 里的 `os.umask(0o002)` → `test_umask_is_set_to_002` 必须红
2. 去掉一次性模式的 `umask 002` → `test_umask_covers_both_daemon_and_oneshot` 必须红
3. 把 `exec python3` 改成 `python3` → `test_oneshot_python_still_execs` 必须红
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
BWRAP = REPO / "backend" / "sandbox_service" / "executor" / "bwrap.py"


def _pytest(tests: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q", "--no-header", "-p", "no:randomly"],
        cwd=REPO / "backend", capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace", env=PYTEST_ENV,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _inject_and_run(old: str, new: str, tests: list[str]) -> tuple[str, int, str]:
    orig = BWRAP.read_text(encoding="utf-8")
    if old not in orig:
        return "inject_failed", -1, "找不到注入片段"
    patched = orig.replace(old, new, 1)
    if patched == orig:
        return "inject_failed", -1, "replace 没有改变内容"
    try:
        BWRAP.write_text(patched, encoding="utf-8", newline="\n")
        rc, out = _pytest(tests)
        return "ran", rc, out
    finally:
        BWRAP.write_text(orig, encoding="utf-8", newline="\n")


def main() -> int:
    problems: list[str] = []
    checked = 0

    # ---- 用例 0：基线必须全绿 ----
    rc, out = _pytest(["tests/test_bwrap_umask.py"])
    if rc == 0:
        print("[✓ 基线] 未修改时全绿")
        checked += 1
    else:
        problems.append(f"基线应全绿，实际 rc={rc}\n{out[-800:]}")
        print("[✗ 基线] 没改就红了")

    # ---- 用例 1：去掉 daemon 的 os.umask ----
    status, rc, out = _inject_and_run(
        "        os.umask(0o002)",
        "        pass  # 注入缺陷：不设 umask",
        ["tests/test_bwrap_umask.py::test_umask_is_set_to_002"],
    )
    if status == "inject_failed":
        problems.append(f"用例1 注入失败：{out}")
        print(f"[✗ 用例1] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例1] 去掉 os.umask → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例1：去掉 os.umask 后仍绿 —— 断言无因果力")
        print("[✗ 用例1] 去掉 os.umask 仍然绿")

    # ---- 用例 2：去掉一次性模式的 umask ----
    #
    # ⚠️ 锚点要短、且**只此一处**。第一次写的是整行含 `\"$0\"` 的 argv，
    # 反斜杠在字符串里多一层转义就对不上，replace 静默不动，
    # 于是「注入失败」被记成了「测试仍绿」—— 又一个假信号。
    # 现在用 `"umask 002; "` 这个只出现在一次性模式那一处的片段。
    status, rc, out = _inject_and_run(
        '"umask 002; exec python3',
        '"umask 000; exec python3',
        ["tests/test_bwrap_umask.py"],
    )
    if status == "inject_failed":
        problems.append(f"用例2 注入失败：{out}")
        print(f"[✗ 用例2] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例2] 去掉一次性模式 umask → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例2：去掉一次性模式 umask 后仍绿 —— 覆盖断言无因果力")
        print("[✗ 用例2] 去掉一次性模式 umask 仍然绿")

    # ---- 用例 3：去掉 exec ----
    status, rc, out = _inject_and_run(
        "exec python3",
        "python3",
        ["tests/test_bwrap_umask.py::test_oneshot_python_still_execs"],
    )
    if status == "inject_failed":
        problems.append(f"用例3 注入失败：{out}")
        print(f"[✗ 用例3] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例3] 去掉 exec → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例3：去掉 exec 后仍绿 —— 断言无因果力")
        print("[✗ 用例3] 去掉 exec 仍然绿")

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