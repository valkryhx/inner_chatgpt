"""反向确认：真 daemon 清理测试在 test 镜像里**能不能变红**。

## 为什么这条单独存在

`test_destroy_leaves_no_process_for_workspace` 是唯一能抓住
「bwrap 内层进程变孤儿」的测试（实测过：`kill` 只杀外层时它报
`残留进程 [8]`）。但它自己也可能假绿——比如 `pids_for` 找错了进程、
或者容器里根本没起 bwrap。

所以要证明两件事，**都必须成立**：

1. 正常代码下它**绿**（说明断言找得到进程，不是空过）
2. 把 `killpg` 拆回 `proc.kill()` 后它**红**（说明它真在盯这条线）

第 1 条不能省：一个「永远找不到进程、于是 `left == []` 恒成立」的
测试会永远绿，而它对孤儿毫无感知。本仓库踩过多次这类假绿。

## 跑法

    python docs/probes/20261119-镜像内测试反向确认.py

需要：test 镜像已构建（`--target test`），Docker 可用。
期望 `ok: 2/2`。退出码非 0 表示反向确认失败。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

IMAGE = "hlz-sandbox-test:latest"
TEST_PATH = "tests/test_bwrap_daemon.py"
TEST_NAME = "test_destroy_leaves_no_process_for_workspace"

# 破坏：把 killpg 整组清理换回单 pid kill（= 修复前的实现）
ORIGINAL = '''        pgid = self.proc.pid          # start_new_session=True ⇒ pid == pgid
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            # 组已经没了 / 不在同一用户下：退回单 pid kill
            try:
                self.proc.kill()
            except ProcessLookupError:
                return'''

BROKEN = '''        try:
            self.proc.kill()
        except ProcessLookupError:
            return'''


def find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "backend" / "sandbox_service" / "executor" / "bwrap.py").exists():
            return p
    raise SystemExit(f"找不到仓库根（从 {start} 往上）")


ROOT = find_repo_root(Path(__file__).resolve())
BWARP = ROOT / "backend" / "sandbox_service" / "executor" / "bwrap.py"
TESTS_DIR = ROOT / "backend" / "tests"


def winpath(p: Path) -> str:
    """转成 Windows 路径给 docker -v 用（Git Bash 的 MSYS 转换会搞坏 POSIX 路径）。"""
    if sys.platform == "win32":
        return str(p).replace("\\", "/")
    return str(p)


def run_in_image(*, bwrap_override: Path | None, extra_args: list[str]) -> tuple[int, str]:
    argv = [
        "docker", "run", "--rm", "--user", "1000:1000",
        "-v", f"{winpath(TESTS_DIR)}:/app/tests:ro",
    ]
    if bwrap_override is not None:
        argv += ["-v", f"{winpath(bwrap_override)}:/app/sandbox_service/executor/bwrap.py:ro"]
    argv += [IMAGE, "python", "-m", "pytest", f"/app/{TEST_PATH}::{TEST_NAME}",
             "-q", "-p", "no:cacheprovider", *extra_args]
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # 先确认镜像在（不在就明确提示，不要让人对着 "No such image" 猜）
    chk = subprocess.run(["docker", "image", "inspect", IMAGE],
                         capture_output=True, text=True)
    if chk.returncode != 0:
        print(f"缺少镜像 {IMAGE}。先构建：")
        print("  docker build -f backend/sandbox_service/Dockerfile "
              "--target test -t hlz-sandbox-test:latest .")
        return 2

    src = BWARP.read_text(encoding="utf-8")
    if ORIGINAL not in src:
        print("bwrap.py 里找不到 killpg 片段（代码已变，请更新本脚本）")
        return 2

    ok = 0

    # ---- 1. 正常代码：必须绿（且不是"找不到进程"的空过）----
    rc, out = run_in_image(bwrap_override=None, extra_args=[])
    if rc == 0 and "1 passed" in out:
        print("[正常代码] ✓ 测试变绿（断言找得到进程）")
        ok += 1
    else:
        print(f"[正常代码] ✗ 期望 1 passed，实际 rc={rc}")
        print(out[-1500:])

    # ---- 2. 破坏 killpg：必须红 ----
    with tempfile.TemporaryDirectory() as td:
        broken = Path(td) / "bwrap_broken.py"
        broken.write_text(src.replace(ORIGINAL, BROKEN, 1), encoding="utf-8", newline="\n")
        rc, out = run_in_image(bwrap_override=broken, extra_args=[])
        if rc != 0 and "1 failed" in out and "残留进程" in out:
            print("[拆掉 killpg] ✓ 测试变红（报残留进程）")
            ok += 1
        else:
            print(f"[拆掉 killpg] ✗ 期望 1 failed 且提到「残留进程」，实际 rc={rc}")
            print(out[-1500:])

    print(f"ok: {ok}/2")
    return 0 if ok == 2 else 1


if __name__ == "__main__":
    sys.exit(main())