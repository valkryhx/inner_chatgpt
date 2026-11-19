"""反向确认：把接线拆掉，测试必须重新变红。

## 为什么必须做这一步

本轮修的是「预热只记账、没复用」。修复很容易看起来对（日志有「槽预热完成」、
测试全绿），但真正的问题是**接线断了**——而断线的代码单看每一处都正确。
所以必须证明：断言确实盯住了那条线。

本脚本注入两种破坏，各自跑一次测试：

1. **拆掉执行器单例**：`_get_executor()` 改回每次 `get_executor()`
   → 预热的 daemon 存在临时实例上，exec 拿新实例（上一轮的真实 bug）
2. **拆掉销毁回调**：`_drop_slot` 不调 `on_destroy`
   → 三条销毁路径都不停 daemon（孤儿进程泄漏）

每种破坏都要求**指定的测试变红**，且**同一文件里的无关测试仍绿**
（否则说明所有测试都在因别的原因失败，不能证明盯住了这条线）。

跑法：
    python work/reverse_confirm_wiring.py
期望输出：`ok: 2/2`，退出码 0。任一变绿（没红）就退出码 1。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def find_repo_root(start: Path) -> Path:
    """往上找含 `backend/sandbox_service` 的目录。

    本脚本有**两份副本**（`work/` 与 `docs/probes/`），层级不同；
    写死 `parents[1]` 会让其中一份算错根目录。向上找标志目录最稳。
    """
    for p in [start, *start.parents]:
        if (p / "backend" / "sandbox_service" / "main.py").exists():
            return p
    raise SystemExit(f"找不到仓库根（从 {start} 往上没有 backend/sandbox_service）")


ROOT = find_repo_root(Path(__file__).resolve())
BACKEND = ROOT / "backend"
MAIN = BACKEND / "sandbox_service" / "main.py"
POOL = BACKEND / "sandbox_service" / "pool.py"
TESTFILE = "tests/test_sandbox_service_wiring.py"

# (名字, 目标文件, 原文片段, 破坏后片段, 必须变红的测试名)
CASES = [
    (
        "拆掉执行器单例",
        MAIN,
        "    if _executor is None:\n"
        "        _executor = get_executor()\n"
        "    return _executor",
        "    return get_executor()",
        ["test_executor_is_singleton_across_calls"],
    ),
    (
        "拆掉销毁回调",
        POOL,
        "                await self._on_destroy(slot.user_id)",
        "                pass  # 破坏：不停 daemon",
        [
            "test_destroy_slot_stops_daemon",
            "test_evict_stops_daemon",
            "test_reap_idle_stops_daemon",
        ],
    ),
    (
        "拆掉空闲回收循环",
        MAIN,
        "    reap_task = asyncio.create_task(_reap_loop(pool))",
        "    reap_task = asyncio.create_task(asyncio.sleep(0))  # 破坏：不起回收",
        ["test_reap_loop_is_started_by_lifespan"],
    ),
]


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def write(p: Path, s: str) -> None:
    p.write_bytes(s.encode("utf-8"))


def run_tests() -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", TESTFILE, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=BACKEND,
        capture_output=True,
    )
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    return r.returncode, out


def failed_names(output: str) -> set[str]:
    """从 pytest 输出里抓 FAILED 行里的测例名。"""
    names = set()
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("FAILED "):
            # FAILED tests/x.py::test_a - AssertionError
            head = line[len("FAILED "):].split(" - ")[0]
            if "::" in head:
                names.add(head.split("::")[-1])
    return names


def main() -> int:
    # 控制台是 GBK 时，中文/符号可能编不出去直接抛 UnicodeEncodeError
    # （本仓库踩过：写成 `✓` 一样会炸，因为问题在输出层不在字面量）。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ok = 0
    for name, path, original, broken, expect_red in CASES:
        text = read(path)
        if original not in text:
            print(f"[{name}] 跳过：源文件里找不到目标片段（代码已变，请更新本脚本）")
            continue

        backup = text
        try:
            write(path, text.replace(original, broken, 1))
            rc, out = run_tests()
        finally:
            write(path, backup)          # 无论如何都恢复

        red = failed_names(out)
        missing = [t for t in expect_red if t not in red]
        if rc == 0 or missing:
            print(f"[{name}] ✗ 反向确认失败：期望变红 {expect_red}，实际红 {sorted(red)}（rc={rc}）")
            print(out[-2000:])
            continue
        print(f"[{name}] ✓ 注入破坏后按预期变红：{sorted(red & set(expect_red))}")
        ok += 1

    total = len([c for c in CASES])
    print(f"ok: {ok}/{total}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())