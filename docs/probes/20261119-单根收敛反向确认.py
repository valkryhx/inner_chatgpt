"""反向确认：单根收敛被破坏时，测试必须变红。

## 三种破坏，对应这次收敛要防的三件事

1. **把白名单分支加回 `workspaces_root`** → 白名单又影响路径（本次事故根因）
2. **把两个根分回去** → app 与沙箱服务算出不同路径
3. **compose 两侧默认值写法不同** → 容器里看到不同的配置（事故直接成因）

每种破坏都必须让**指定测试变红**。

跑法：
    python docs/probes/20261119-单根收敛反向确认.py
期望 `ok: 3/3`。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "backend" / "app" / "workspace.py").exists():
            return p
    raise SystemExit(f"找不到仓库根（从 {start} 往上）")


ROOT = find_repo_root(Path(__file__).resolve())
BACKEND = ROOT / "backend"
APP_WS = BACKEND / "app" / "workspace.py"
SVC_WS = BACKEND / "sandbox_service" / "workspace.py"
COMPOSE = BACKEND / "deploy" / "docker-compose.yml"
TESTFILE = "tests/test_single_workspace_root.py"

# (名字, 文件, 原文, 破坏后, 必须变红)
CASES = [
    (
        "白名单分支加回 workspaces_root",
        APP_WS,
        "    _reject_unsafe_segment(user_id)\n    return WORKSPACES_DIR / user_id",
        "    _reject_unsafe_segment(user_id)\n"
        "    import os as _os\n"
        "    _wl = (_os.environ.get('HLZ_SANDBOX_MVP_USERS') or '').split(',')\n"
        "    if user_id in [u.strip() for u in _wl if u.strip()]:\n"
        "        return WORKSPACES_DIR.parent / 'sandbox' / user_id\n"
        "    return WORKSPACES_DIR / user_id",
        ["test_whitelist_does_not_affect_path"],
    ),
    (
        "沙箱服务侧路径分叉（与 app 不一致）",
        SVC_WS,
        "    reject_unsafe_segment(user_id)\n    return workspaces_dir() / user_id",
        "    reject_unsafe_segment(user_id)\n"
        "    import os as _os\n"
        "    _wl = (_os.environ.get('HLZ_SANDBOX_MVP_USERS') or '').split(',')\n"
        "    if user_id in [u.strip() for u in _wl if u.strip()]:\n"
        "        return workspaces_dir().parent / 'sandbox' / user_id\n"
        "    return workspaces_dir() / user_id",
        ["test_both_sides_agree_regardless_of_whitelist"],
    ),
    (
        "compose 两侧 MVP_USERS 默认值写法不同",
        COMPOSE,
        "      # 白名单：写法必须与 app 服务上那条**逐字相同**（见 app 服务的注释）。",
        "      HLZ_SANDBOX_MVP_USERS: ${HLZ_SANDBOX_MVP_USERS-u_aaa,u_bbb}\n"
        "      # 白名单：写法必须与 app 服务上那条**逐字相同**（见 app 服务的注释）。",
        ["test_compose_mvp_users_has_identical_default_on_both_services"],
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