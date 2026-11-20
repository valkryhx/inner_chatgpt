"""反向确认：证明「门禁已删除」的测试真的能抓到门禁被加回来。

## 三个用例

0. **不改任何东西 → 必须全绿**（没有这条，「全红」不携带信息）
1. 把 `registry.check` 加回 `_write_impl` → 覆盖写测试必须变红
2. 把 `registry.check` 加回 `_patch_impl` → patch 无读测试必须变红
3. 把 `DEFAULT_READ_LINES` 改小并重新加门禁 → 大文件测试必须变红
   （这条验的是「门槛对大文件必然失败」这个论断本身）

## 为什么要用脚本注入而不是手改

手改容易忘恢复，而一旦把门禁留在代码里，后续所有「写入成功」的
观测都会被污染。脚本 finally 恢复原文件。
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
BT = REPO / "backend" / "app" / "builtin_tools.py"
TARGET = "tests/test_no_read_before_write.py"


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
        return "inject_failed", -1, "找不到注入片段"
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

    # ---- 用例 0：基线必须全绿 ----
    rc, out = _pytest([TARGET])
    if rc == 0:
        print("[✓ 基线] 未修改时全绿")
        checked += 1
    else:
        problems.append(f"基线应全绿，实际 rc={rc}\n{out[-800:]}")
        print("[✗ 基线] 没改就红了")

    # ---- 用例 1：把门禁加回 _write_impl ----
    status, rc, out = _inject_and_run(
        """    if mode not in ("overwrite", "append", "prepend"):
        return _err(f"不支持的 mode: {mode}（overwrite/append/prepend）")
    target.parent.mkdir(parents=True, exist_ok=True)""",
        """    if mode not in ("overwrite", "append", "prepend"):
        return _err(f"不支持的 mode: {mode}（overwrite/append/prepend）")
    if mode == "overwrite" and target.exists():
        from .read_registry import ReadRegistry
        _r = ReadRegistry()
        _ok, _why = _r.check(target)
        if not _ok:
            return _err(_why)
    target.parent.mkdir(parents=True, exist_ok=True)""",
        [f"{TARGET}::test_overwrite_existing_without_read_is_allowed"],
    )
    if status == "inject_failed":
        problems.append(f"用例1 注入失败：{out}")
        print(f"[✗ 用例1] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例1] 加回覆盖写门禁 → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例1：加回门禁后仍绿 —— 断言无因果力")
        print("[✗ 用例1] 加回门禁仍然绿")

    # ---- 用例 2：把门禁加回 _patch_impl ----
    status, rc, out = _inject_and_run(
        """    if not target.is_file():
        return _err(f"文件不存在: {path}")
    if not old_content:""",
        """    if not target.is_file():
        return _err(f"文件不存在: {path}")
    from .read_registry import ReadRegistry
    _ok, _why = ReadRegistry().check(target)
    if not _ok:
        return _err(_why)
    if not old_content:""",
        [f"{TARGET}::test_patch_without_prior_read_is_allowed"],
    )
    if status == "inject_failed":
        problems.append(f"用例2 注入失败：{out}")
        print(f"[✗ 用例2] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例2] 加回 patch 门禁 → 变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例2：加回 patch 门禁后仍绿 —— 断言无因果力")
        print("[✗ 用例2] 加回 patch 门禁仍然绿")

    # ---- 用例 3：大文件门槛（把 DEFAULT_READ_LINES 缩到 10 + 加回门禁）----
    orig = BT.read_text(encoding="utf-8")
    patched = orig.replace("DEFAULT_READ_LINES = 200", "DEFAULT_READ_LINES = 10", 1)
    patched = patched.replace(
        """    if mode not in ("overwrite", "append", "prepend"):
        return _err(f"不支持的 mode: {mode}（overwrite/append/prepend）")
    target.parent.mkdir(parents=True, exist_ok=True)""",
        """    if mode not in ("overwrite", "append", "prepend"):
        return _err(f"不支持的 mode: {mode}（overwrite/append/prepend）")
    if mode == "overwrite" and target.exists():
        from .read_registry import ReadRegistry
        _ok, _why = ReadRegistry().check(target)
        if not _ok:
            return _err(_why)
    target.parent.mkdir(parents=True, exist_ok=True)""",
        1,
    )
    if patched == orig:
        problems.append("用例3 注入失败：替换没有改变内容")
        print("[✗ 用例3] 注入失败")
    else:
        try:
            BT.write_text(patched, encoding="utf-8", newline="\n")
            rc, out = _pytest([f"{TARGET}::test_overwrite_large_unread_file_is_allowed"])
        finally:
            BT.write_text(orig, encoding="utf-8", newline="\n")
        if rc != 0:
            print(f"[✓ 用例3] 大文件 + 门禁 → 变红 (rc={rc})")
            checked += 1
        else:
            problems.append("用例3：大文件门槛未生效 —— 「必然失败」这个论断不成立")
            print("[✗ 用例3] 大文件仍然绿")

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