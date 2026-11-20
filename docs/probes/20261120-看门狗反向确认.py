"""反向确认：看门狗测试真的能抓到「没有看门狗」。

## 四个用例

0. **基线不改 → 必须全绿**
1. 把看门狗阈值设成 0（等于关闭）→ 卡住测试必须红
2. 让 `publish()` 不重置心跳 → 「有进展不该杀」的测试必须红（会误杀）
3. 把闲置判定改成「总时长」→ 有进展的长任务必须红
4. 看门狗不 cancel 主任务（只 return）→ 卡住测试必须红

## 为什么第 2、3 条分开

它们是两个**相反方向**的失效：
- 方向 A（该杀不杀）：卡住的 job 一直挂着 → 用户看永不结束的回复
- 方向 B（不该杀却杀）：合法的慢任务被砍断 → 用户任务失败

只测 A 会让「阈值调得过小」这类错误蒙混过关。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

PYTEST_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}
TARGET = "tests/test_turn_job_watchdog.py"
TARGET_E2E = "tests/test_turn_job_watchdog_e2e.py"


def _find_repo_root(start: pathlib.Path) -> pathlib.Path:
    for p in [start, *start.parents]:
        if (p / "backend" / "app" / "workspace.py").is_file():
            return p
    raise SystemExit(f"找不到仓库根（从 {start} 向上）")


REPO = _find_repo_root(pathlib.Path(__file__).resolve().parent)
TJ = REPO / "backend" / "app" / "turn_job.py"


def _pytest(tests: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q", "--no-header", "-p", "no:randomly"],
        cwd=REPO / "backend", capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace", env=PYTEST_ENV,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _inject_and_run(old: str, new: str, tests: list[str]) -> tuple[str, int, str]:
    orig = TJ.read_text(encoding="utf-8")
    if old not in orig:
        return "inject_failed", -1, f"找不到注入片段：{old[:80]!r}"
    patched = orig.replace(old, new, 1)
    if patched == orig:
        return "inject_failed", -1, "replace 没有改变内容"
    try:
        TJ.write_text(patched, encoding="utf-8", newline="\n")
        rc, out = _pytest(tests)
        return "ran", rc, out
    finally:
        TJ.write_text(orig, encoding="utf-8", newline="\n")


def main() -> int:
    problems: list[str] = []
    checked = 0

    # ---- 基线 ----
    rc, out = _pytest([TARGET, TARGET_E2E])
    if rc == 0:
        print("[✓ 基线] 未修改时全绿")
        checked += 1
    else:
        problems.append(f"基线应全绿，实际 rc={rc}\n{out[-600:]}")
        print("[✗ 基线] 没改就红了")

    # ---- 用例 1：看门狗永不触发 ----
    status, rc, out = _inject_and_run(
        "        if idle_timeout_s <= 0:\n            return                       # 关闭",
        "        if True:\n            return                       # 注入缺陷：永远关闭",
        [f"{TARGET}::test_watchdog_cancels_stuck_job"],
    )
    if status == "inject_failed":
        problems.append(f"用例1 注入失败：{out}")
        print(f"[✗ 用例1] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例1] 看门狗关闭 → 卡住测试变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例1：看门狗关闭后卡住测试仍绿 —— 无因果力")
        print("[✗ 用例1] 看门狗关闭仍然绿")

    # ---- 用例 2：publish 不重置心跳 → 会误杀 ----
    status, rc, out = _inject_and_run(
        "        self._last_progress = time.monotonic()\n        self.seq += 1",
        "        pass  # 注入缺陷：publish 不重置心跳\n        self.seq += 1",
        [f"{TARGET_E2E}::test_publish_between_waits_keeps_job_alive_longer_than_timeout"],
    )
    if status == "inject_failed":
        problems.append(f"用例2 注入失败：{out}")
        print(f"[✗ 用例2] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例2] publish 不重置心跳 → 误杀测试变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例2：心跳失效后仍绿 —— 「有进展不该杀」没被测到")
        print("[✗ 用例2] 心跳失效仍然绿")

    # ---- 用例 3：闲置判定改成总时长 ----
    status, rc, out = _inject_and_run(
        "            idle = job.idle_seconds()",
        "            idle = job.idle_seconds() if job.seq == 0 else 999999  # 注入：误用总时长",
        [f"{TARGET_E2E}::test_publish_between_waits_keeps_job_alive_longer_than_timeout"],
    )
    if status == "inject_failed":
        problems.append(f"用例3 注入失败：{out}")
        print(f"[✗ 用例3] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例3] 误用总时长 → 误杀测试变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例3：误用总时长后仍绿 —— 两个方向没都被测到")
        print("[✗ 用例3] 误用总时长仍然绿")

    # ---- 用例 4：看门狗只标记不 cancel ----
    status, rc, out = _inject_and_run(
        "            job.mark_watchdog(reason)\n            target.cancel()",
        "            job.mark_watchdog(reason)  # 注入缺陷：只标记不中止",
        [f"{TARGET}::test_watchdog_cancels_stuck_job"],
    )
    if status == "inject_failed":
        problems.append(f"用例4 注入失败：{out}")
        print(f"[✗ 用例4] 注入失败：{out}")
    elif rc != 0:
        print(f"[✓ 用例4] 只标记不中止 → 卡住测试变红 (rc={rc})")
        checked += 1
    else:
        problems.append("用例4：只标记不中止仍绿 —— job 会永远挂着")
        print("[✗ 用例4] 只标记不中止仍然绿")

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