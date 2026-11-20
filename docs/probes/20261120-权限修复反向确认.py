"""反向确认：证明权限测试真的能抓到缺失的修复。

## 为什么必须做这一步

`test_ensure_dir_shared_repairs_legacy_dirs` 这类测试很容易「因错误原因变绿」：
目录本来就存在、chmod 恰好成功……都会让断言过。所以按 AGENTS.md §8.5，
把实现临时改回「只建不修」跑一次，必须变红。

## 四个用例

0. **不改任何东西 → 必须全绿**（没有这条，「全红」不携带信息）
1. `_mkdir_and_fix` 不修 mode → 遗留目录的 g+w/setgid 断言必须失败
2. `_mkdir_and_fix` 不 chgrp → 属组断言必须失败
3. `write_file` 分发前不调 ensure_dir_shared → 守卫必须失败

## ⚠️ 第一版这个探针自己是假绿

第一版三项全是「[GREEN-意外]」，看起来像断言没有因果力。
真相是**注入没匹配上**：`_run_patched` 在 `old not in orig` 时返回 `rc=-1`，
而判断写的是 `if rc != 0: 期望红` —— `-1` 也算「非 0」，于是**注入失败的
情况被记成了「成功变红」**…反过来，如果注入字符串写错导致 pytest 根本没跑，
`.replace()` 静默不动，测试仍然全绿，就被报成「断言无因果力」。

这类「探针自己算错，把无效信号当成有效信号」正是 AGENTS.md §8.6 说的假信号。
修法：注入必须**断言替换真的发生了**（`patched != orig`），并单独区分
「注入失败」和「测试仍绿」两种结局。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

def _find_repo_root(start: pathlib.Path) -> pathlib.Path:
    """向上找含 `backend/app/workspace.py` 的那一层。

    ⚠️ 不能写死 `parents[1]`：这个探针曾在 `work/`（两层）和
    `docs/probes/`（两层）之间搬过，写死层级就会解析成
    `docs/backend` 这种不存在的目录 —— 实测报
    `NotADirectoryError: [WinError 267] 目录名称无效`。
    """
    for p in [start, *start.parents]:
        if (p / "backend" / "app" / "workspace.py").is_file():
            return p
    raise SystemExit(f"找不到仓库根（从 {start} 向上没有 backend/app/workspace.py）")


REPO = _find_repo_root(pathlib.Path(__file__).resolve().parent)
WS = REPO / "backend" / "app" / "workspace.py"
MG = REPO / "backend" / "app" / "sandbox" / "manager.py"

PYTEST_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def _pytest(tests: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q", "--no-header", "-p", "no:randomly"],
        cwd=REPO / "backend", capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace",
        env=PYTEST_ENV,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _inject_and_run(mod: pathlib.Path, old: str, new: str,
                    tests: list[str]) -> tuple[str, int, str]:
    """注入 → 跑测试 → 还原。

    Returns:
        ("inject_failed" | "ran", rc, output)
    """
    orig = mod.read_text(encoding="utf-8")
    if old not in orig:
        return "inject_failed", -1, f"{mod.name} 里找不到注入片段"
    patched = orig.replace(old, new, 1)
    if patched == orig:                      # 双保险：替换必须是真变化
        return "inject_failed", -1, "replace 没有改变内容"
    try:
        mod.write_text(patched, encoding="utf-8", newline="\n")
        rc, out = _pytest(tests)
        return "ran", rc, out
    finally:
        mod.write_text(orig, encoding="utf-8", newline="\n")


def main() -> int:
    problems: list[str] = []
    checked = 0

    # ---------- 用例 0：基线必须全绿 ----------
    rc, out = _pytest(["tests/test_shared_group_permissions.py"])
    if rc == 0:
        print("[✓ 基线] 未修改时全绿")
        checked += 1
    else:
        problems.append(f"基线应全绿，实际 rc={rc}\n{out[-800:]}")
        print("[✗ 基线] 没改就红了")

    # ---------- 用例 1：不修 mode ----------
    status, rc, out = _inject_and_run(
        WS,
        old="    want = current | 0o2000 | 0o020       # setgid + g+w，保留其余位",
        new="    return  # 注入缺陷：只 mkdir，不修 mode\n"
            "    want = current | 0o2000 | 0o020       # setgid + g+w，保留其余位",
        tests=["tests/test_shared_group_permissions.py::test_mkdir_and_fix_calls_chmod_when_mode_differs",
               "tests/test_shared_group_permissions.py::test_ensure_dir_shared_repairs_legacy_dirs"],
    )
    if status == "inject_failed":
        problems.append(f"用例1 注入失败：{out}")
        print(f"[✗ 用例1] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例1] 不修 mode → 测试变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例1：注入「不修 mode」后测试仍绿 —— 断言无因果力")
        print("[✗ 用例1] 不修 mode 仍然绿")

    # ---------- 用例 2：不 chgrp ----------
    status, rc, out = _inject_and_run(
        WS,
        old="                chown(p, -1, gid)          # -1 = 不动属主",
        new="                pass  # 注入缺陷：不 chgrp",
        tests=["tests/test_shared_group_permissions.py::test_mkdir_and_fix_calls_chown_when_gid_differs"],
    )
    if status == "inject_failed":
        problems.append(f"用例2 注入失败：{out}")
        print(f"[✗ 用例2] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例2] 不 chgrp → 测试变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例2：注入「不 chgrp」后测试仍绿 —— 属组断言无因果力")
        print("[✗ 用例2] 不 chgrp 仍然绿")

    # ---------- 用例 3：分发前不建目录 ----------
    status, rc, out = _inject_and_run(
        MG,
        old="            ensure_dir_shared(target.parent,\n"
            "                              boundary=user_workspace(user_id, create=False))\n"
            "            rel = _workspace_rel(user_id, target, root)\n"
            "            try:\n"
            "                handled, note = await backend.write_file(",
        new="            rel = _workspace_rel(user_id, target, root)\n"
            "            try:\n"
            "                handled, note = await backend.write_file(",
        tests=["tests/test_shared_group_permissions.py::test_backend_dispatch_ensures_dirs_before_calling_service"],
    )
    if status == "inject_failed":
        problems.append(f"用例3 注入失败：{out}")
        print(f"[✗ 用例3] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例3] 分发前不建目录 → 守卫变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例3：注入「分发前不建目录」后守卫仍绿 —— 守卫无因果力")
        print("[✗ 用例3] 分发前不建目录仍然绿")

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