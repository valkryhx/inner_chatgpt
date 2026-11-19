"""反向确认：strict 安全语义的两处守卫被拆掉时，测试必须变红。

## 两处缺陷，各一种破坏

1. **可用性探针退回 Docker** → service 部署下永远误判
2. **strict 下允许回落进程内** → 无隔离执行且不告警（本仓库实测到的漏洞）

跑法：
    python docs/probes/20261119-strict语义反向确认.py
期望 `ok: 3/3`。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "backend" / "app" / "sandbox" / "manager.py").exists():
            return p
    raise SystemExit(f"找不到仓库根（从 {start} 往上）")


ROOT = find_repo_root(Path(__file__).resolve())
BACKEND = ROOT / "backend"
MANAGER = BACKEND / "app" / "sandbox" / "manager.py"
TOOLS = BACKEND / "app" / "builtin_tools.py"
TESTFILE = "tests/test_sandbox_strict_mode.py"

PROBE_ORIGINAL = """        backend = _selected_backend()
        if backend is None:
            # `backend=inproc`（回滚路径）：那条路确实用 Docker，探针是对的。
            ok, reason = probe.check(self.cfg)
            return None if ok else reason
        try:
            ok, reason = await backend.health()
        except Exception as exc:  # noqa: BLE001
            # 自检本身抛异常 = 不可用。不能让一个坏掉的后端把判断变成「可用」。
            return f"{type(exc).__name__}: {exc}"
        return None if ok else reason"""

BYPASS_ORIGINAL = """    from .sandbox.config import get_config

    mode = get_config().mode
    if mode == "strict":"""

CASES = [
    (
        "可用性探针退回 Docker（只探 Docker，不问后端）",
        MANAGER,
        PROBE_ORIGINAL,
        "        backend = _selected_backend()\n"
        "        if backend is None:\n"
        "            # `backend=inproc`（回滚路径）：那条路确实用 Docker，探针是对的。\n"
        "            ok, reason = probe.check(self.cfg)\n"
        "            return None if ok else reason\n"
        "        ok, reason = probe.check(self.cfg)   # 破坏：不问后端\n"
        "        return None if ok else reason",
        ["test_degrade_reason_asks_the_selected_backend"],
    ),
    (
        "auto 也拒绝回落（过度收紧）",
        TOOLS,
        BYPASS_ORIGINAL,
        "    from .sandbox.config import get_config\n\n"
        "    get_config()  # 破坏：把 mode 写死成 strict，任何模式都不许回落\n"
        "    mode = \"strict\"\n"
        "    if mode == \"strict\":",
        # ⚠️ 不能写 test_auto_still_degrades_and_executes：那条走的是
        # `handled=False` 分支（manager 返回降级标记），**不经过 except**，
        # 所以把 bypass 守卫改坏也不会让它变红（第一版写错在这）。
        # 真正经过 except 的 auto 用例在 seams 文件里。
        ["test_sandbox_exception_falls_back_in_auto"],
    ),
    (
        "strict 下允许回落（原始漏洞）",
        TOOLS,
        BYPASS_ORIGINAL,
        "    from .sandbox.config import get_config\n\n"
        "    get_config()  # 破坏：把 mode 写死成 auto，strict 也当 auto 处理\n"
        "    mode = \"auto\"\n"
        "    if mode == \"strict\":",
        ["test_strict_does_not_execute_when_backend_unavailable",
         "test_sandbox_exception_does_not_fall_back_in_strict"],
    ),
]


def run_tests(extra: list[str]) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", TESTFILE, *extra,
         "-q", "--no-header", "-p", "no:cacheprovider"],
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
    for name, path, original, broken, expect_red in CASES:
        text = path.read_text(encoding="utf-8")
        if original not in text:
            print(f"[{name}] 跳过：找不到目标片段（代码已变，请更新脚本）")
            continue
        try:
            path.write_text(text.replace(original, broken, 1), encoding="utf-8")
            # 目标用例若在 seams 文件里，一并跑（两个文件都跑最稳）
            extra = ["tests/test_sandbox_seams.py"]
            _rc, out = run_tests(extra)
        finally:
            path.write_text(text, encoding="utf-8")

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