"""反向确认：Dockerfile stage 顺序被破坏时，测试必须变红。

## 三种破坏，对应三种真实风险

1. **把 test stage 挪到最后** → `docker compose build` 默认构建最后一个 stage，
   生产镜像带上 pytest（只在 `pip list` 里多一行，日志/启动/隔离全正常）
2. **删掉 test stage** → 8 条 requires_bwrap 测试又变成"任何环境都不跑"
3. **让 production 也装 pytest** → 生产镜像变脏

每种破坏都必须让**指定的测试变红**；同类无关测试仍绿（否则说明是别的原因失败）。

跑法：
    python docs/probes/20261119-镜像stage反向确认.py
期望 `ok: 3/3`。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "backend" / "sandbox_service" / "Dockerfile").exists():
            return p
    raise SystemExit(f"找不到仓库根（从 {start} 往上没有 backend/sandbox_service/Dockerfile）")


ROOT = find_repo_root(Path(__file__).resolve())
BACKEND = ROOT / "backend"
DOCKERFILE = BACKEND / "sandbox_service" / "Dockerfile"
TESTFILE = "tests/test_sandbox_image_targets.py"

# (名字, 原文片段, 破坏后, 必须变红的测试)
CASES = [
    (
        "把 test stage 挪到最后（去掉 production）",
        "FROM base AS production",
        "FROM base AS _unused_marker",   # 让 production 消失 == 最后一个是 test
        ["test_has_production_stage", "test_production_is_last_stage"],
    ),
    (
        "删掉 test stage 的 pytest",
        "RUN pip install --no-cache-dir pytest",
        "RUN echo skipped",
        ["test_test_stage_installs_pytest"],
    ),
    (
        "让 base 也装 pytest（production 会继承）",
        "FROM python:3.12-slim AS base",
        "FROM python:3.12-slim AS base\nRUN pip install --no-cache-dir pytest",
        ["test_base_stage_does_not_install_pytest", "test_no_stray_pytest_install_outside_test_stage"],
    ),
]


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


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
        text = read(DOCKERFILE)
        if original not in text:
            print(f"[{name}] 跳过：Dockerfile 里找不到目标片段（代码已变，请更新本脚本）")
            continue
        try:
            DOCKERFILE.write_text(text.replace(original, broken, 1), encoding="utf-8")
            _rc, out = run_tests()
        finally:
            DOCKERFILE.write_text(text, encoding="utf-8")   # 无论如何都恢复

        red = failed_names(out)
        missing = [t for t in expect_red if t not in red]
        if missing:
            print(f"[{name}] ✗ 期望变红 {expect_red}，实际红 {sorted(red)}")
            print(out[-1500:])
            continue
        print(f"[{name}] ✓ 按预期变红：{sorted(red & set(expect_red))}")
        ok += 1

    print(f"ok: {ok}/{len(CASES)}")
    return 0 if ok == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())