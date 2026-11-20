"""反向确认：文件工具的 backend 分发被拆掉时，测试必须变红。

## 三种破坏

1. 拆掉 `write_file` 的分发 → 走 Docker → 本机没 docker SDK 或容器不可用
2. 拆掉 `patch_file` 的分发 → 同上（防止只修一半）
3. 服务侧跳过路径校验 → 任意路径可写（安全漏洞）

跑法：
    python docs/probes/20261120-文件工具分发反向确认.py
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
SVC_WS = BACKEND / "sandbox_service" / "workspace.py"
WS_MOD = BACKEND / "app" / "workspace.py"

WRITE_DISPATCH = """        backend = _selected_backend()
        if backend is not None and backend.name != "inproc":
            rel = _workspace_rel(user_id, target, root)
            try:
                handled, note = await backend.write_file("""

PATCH_DISPATCH = """        backend = _selected_backend()
        if backend is not None and backend.name != "inproc":
            rel = _workspace_rel(user_id, target, root)
            try:
                handled, note = await backend.patch_file("""

CASES = [
    (
        "拆掉 write_file 的分发",
        MANAGER,
        WRITE_DISPATCH,
        "        backend = None\n"
        "        if backend is not None and backend.name != \"inproc\":\n"
        "            rel = _workspace_rel(user_id, target, root)\n"
        "            try:\n"
        "                handled, note = await backend.write_file(",
        ["test_write_file_dispatches_to_service_backend"],
    ),
    (
        "拆掉 patch_file 的分发",
        MANAGER,
        PATCH_DISPATCH,
        "        backend = None\n"
        "        if backend is not None and backend.name != \"inproc\":\n"
        "            rel = _workspace_rel(user_id, target, root)\n"
        "            try:\n"
        "                handled, note = await backend.patch_file(",
        ["test_patch_file_dispatches_to_service_backend"],
    ),
    (
        "建目录不用 ensure_dir_shared（沙箱写不进去）",
        WS_MOD,
        "    ensure_dir_shared(target)",
        "    target.mkdir(parents=True, exist_ok=True)   # 破坏：绕过共享辅助函数",
        # 用静态测试而非 POSIX 测试：后者在 Windows 上 skip，反向确认就
        # 永远看不到红（第一版写错在这，ok 停在 3/4）。
        ["test_user_workspace_and_write_root_use_same_helper"],
    ),
    (
        "服务侧跳过路径校验",
        SVC_WS,
        "    if target != root and root not in target.parents:\n"
        "        raise PathSecurityError(f\"路径越界（必须位于 {root} 内）: {raw}\")\n"
        "    return target",
        "    return target   # 破坏：不校验越界",
        ["test_write_rejects_parent_escape", "test_write_rejects_deep_escape"],
    ),
]


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def run_tests() -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_sandbox_service_file_ops.py",
         "tests/test_sandbox_service_file_api.py",
         "tests/test_shared_group_permissions.py",
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
        text = read(path)
        if original not in text:
            print(f"[{name}] 跳过：找不到目标片段（代码已变，请更新脚本）")
            continue
        try:
            path.write_text(text.replace(original, broken, 1), encoding="utf-8")
            _rc, out = run_tests()
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