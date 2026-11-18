"""第 8 步：真机隔离验收 + 资源测量（起真容器）。

设计文档 §3「验收标准」的 7 条隔离边界，逐条实测：

| # | 检查项 | 期望 |
| --- | --- | --- |
| 1 | 能读自己的 /ws | 成功 |
| 2 | 读不到宿主其它目录 | 失败/为空 |
| 3 | 看不到其它用户 workspace | 失败 |
| 4 | 容器内是 root 但 cap 全丢 | CapEff 为空/极小 |
| 5 | 网络不可达 | DNS/连接都失败 |
| 6 | 写 /ws 落到宿主 D:\\sandbox\\<uid> | 宿主能看到 |
| 7 | OOM / pids 限制生效 | 触顶被拒 |

资源测量：空闲内存、exec 延迟、创建耗时。

用法（Git Bash，Windows 原生路径）：
    python tests/manual/sandbox_acceptance.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("HLZ_SANDBOX_MODE", "strict")
os.environ.setdefault("HLZ_SANDBOX_IMAGE", "hlz-sandbox:2026-11-18")

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    from app.sandbox.config import get_config
    from app.sandbox.runtime import ContainerRuntime, container_name
    from app.sandbox import probe
    import docker

    cfg = get_config(reload=True)
    print(f"image={cfg.image} mode={cfg.mode}")
    print(f"workspaces_root={os.environ.get('HLZ_SANDBOX_WORKSPACES_DIR', '(default D:\\sandbox)')}")

    ok, reason = probe.check(cfg)
    if not ok:
        print(f"自检失败，终止：{reason}")
        return 2
    print("自检通过\n")

    # ---- 准备两个用户的宿主 workspace ----
    from app.config import SANDBOX_WORKSPACES_DIR
    uid_a = f"u_acc_{uuid.uuid4().hex[:6]}"
    uid_b = f"u_acc_{uuid.uuid4().hex[:6]}"
    ws_a = Path(SANDBOX_WORKSPACES_DIR) / uid_a
    ws_b = Path(SANDBOX_WORKSPACES_DIR) / uid_b
    ws_a.mkdir(parents=True, exist_ok=True)
    ws_b.mkdir(parents=True, exist_ok=True)
    (ws_a / "mine.txt").write_text("secret-of-a", encoding="utf-8")
    (ws_b / "mine.txt").write_text("secret-of-b", encoding="utf-8")

    rt = ContainerRuntime(cfg)
    created: list[str] = []

    def create(uid: str, ws: Path) -> str:
        t0 = time.perf_counter()
        cid = rt.create(uid, str(ws))
        dt = time.perf_counter() - t0
        created.append(cid)
        print(f"  容器 {uid} 创建耗时 {dt*1000:.0f} ms  cid={cid[:12]}")
        return cid

    try:
        print("== 创建两个用户容器 ==")
        cid_a = create(uid_a, ws_a)
        cid_b = create(uid_b, ws_b)

        # ---- 1. 能读自己的 /ws ----
        r = await rt.exec_python(cid_a, "print(open('/ws/mine.txt').read())",
                                 timeout_s=20, host_workspace=str(ws_a))
        record("1 读自己的 /ws", "secret-of-a" in r.stdout, r.stdout.strip()[:60])

        # ---- 2. 宿主其它目录不可见 ----
        r = await rt.exec_shell(cid_a, "ls /d/git_codes 2>&1 | head -3; ls /host 2>&1 | head -2",
                                timeout_s=20)
        leaked = "inner_chatgpt" in r.stdout or "claudecode" in r.stdout
        record("2 宿主其它目录不可见", not leaked, r.stdout.strip()[:60].replace("\n", " | "))

        # ---- 3. 看不到其它用户 workspace ----
        r = await rt.exec_python(
            cid_a,
            "import pathlib\n"
            f"hit = [p for p in pathlib.Path('/').rglob('mine.txt') if 'ws' not in str(p)]\n"
            f"print('SECRET_B' if pathlib.Path('/ws/mine.txt').read_text()=='secret-of-b' else 'ok')\n"
            f"print(len(hit))\n",
            timeout_s=30, host_workspace=str(ws_a))
        other_b = "SECRET_B" in r.stdout
        record("3 看不到其它用户 workspace", not other_b, r.stdout.strip()[:60].replace("\n", " | "))

        # ---- 4. cap 全丢 ----
        r = await rt.exec_shell(cid_a, "grep CapEff /proc/self/status", timeout_s=20)
        cap = ""
        for line in r.stdout.splitlines():
            if line.startswith("CapEff:"):
                cap = line.split(":", 1)[1].strip()
        # 全丢时 CapEff 是 0000000000000000
        record("4 cap 全丢", cap == "0" * 16, f"CapEff={cap}")

        # ---- 5. 网络不可达 ----
        r = await rt.exec_python(
            cid_a,
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 443), timeout=4); print('NET_OPEN')\n"
            "except Exception as e:\n"
            "    print('NET_BLOCKED', type(e).__name__)\n",
            timeout_s=20, host_workspace=str(ws_a))
        record("5 网络不可达", "NET_BLOCKED" in r.stdout, r.stdout.strip()[:60])

        # ---- 6. 写入落到宿主 ----
        marker = uuid.uuid4().hex[:8]
        r = await rt.exec_python(
            cid_a,
            f"open('/ws/from_container.txt','w').write('{marker}')\nprint('written')\n",
            timeout_s=20, host_workspace=str(ws_a))
        host_file = ws_a / "from_container.txt"
        got = host_file.read_text(encoding="utf-8") if host_file.exists() else ""
        record("6 容器写入落到宿主", got == marker, f"{host_file}")

        # ---- 7a. 内存上限 ----
        r = await rt.exec_python(
            cid_a,
            "buf = bytearray()\n"
            "try:\n"
            "    for _ in range(400): buf.extend(b'x' * 10_000_000)\n"
            "    print('NO_OOM', len(buf))\n"
            "except MemoryError:\n"
            "    print('MEMORY_ERROR')\n",
            timeout_s=90, host_workspace=str(ws_a))
        oom_ok = r.exit_code == 137 or "MEMORY_ERROR" in r.stdout or "NO_OOM" not in r.stdout
        record("7a 内存上限生效", oom_ok, f"exit={r.exit_code} {r.stdout.strip()[:40]}")

        # ---- 7b. pids 上限 ----
        r = await rt.exec_shell(
            cid_a,
            "n=0; while [ $n -lt 400 ]; do sleep 60 & n=$((n+1)); done; echo FORKED_$n 2>&1",
            timeout_s=40)
        pids_ok = "FORKED_400" not in r.stdout
        record("7b pids 上限生效", pids_ok, f"exit={r.exit_code} {r.stdout.strip()[:40]}")

        # ---- 资源测量 ----
        print("\n== 资源测量 ==")
        client = docker.from_env()
        try:
            for uid, cid in ((uid_a, cid_a), (uid_b, cid_b)):
                stats = client.containers.get(cid).stats(stream=False)
                mem = stats["memory_stats"].get("usage", 0)
                limit = stats["memory_stats"].get("limit", 0)
                print(f"  {uid}: 内存 {mem/1024/1024:.2f} MiB / limit {limit/1024/1024:.0f} MiB")
            print(f"  容器总数：{len(created)}")
            for _ in range(5):
                t0 = time.perf_counter()
                await rt.exec_python(cid_a, "print(1)", timeout_s=20,
                                     host_workspace=str(ws_a))
                print(f"  exec 延迟 {1000*(time.perf_counter()-t0):.0f} ms")
        finally:
            client.close()

    finally:
        print("\n== 清理 ==")
        for cid in created:
            rt.destroy(cid)
            print(f"  已销毁 {cid[:12]}")
        # 删掉测试用的临时用户目录（只删本次创建的）
        import shutil
        for d in (ws_a, ws_b):
            try:
                shutil.rmtree(d)
                print(f"  已删除测试目录 {d}")
            except OSError:
                pass

    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n== 结果：{len(RESULTS)-len(bad)}/{len(RESULTS)} 通过 ==")
    if bad:
        print("失败项：" + ", ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))