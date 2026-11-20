"""端到端验证：从 app 侧调 write_file，写到遗留 755 root:root 目录下。

## 为什么必须走 app、不能直连沙箱服务

根因就是「只有 app（root）能修 chmod 被拒的目录」。
直连服务测的是另一件事（服务侧能不能写），已经证明它做不到。
这条探针测的是**修复真的发生了**：app 修好 → 服务写成功。

## 判据（必须能区分两种状态）

- 修好前：507 / PermissionError
- 修好后：written=True，且磁盘上文件内容 == 期望
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, "/app/backend")      # 只在 **app 容器内** 跑（见文件头）

USER = "u_c56b8337dd09"
REL = ".subagents/co_8d8bd184324a/writer/trump/trump_report_v2.md"
EXPECT = "# e2e probe\n\napp 分发 → 服务写入 → 遗留目录已修复\n"


async def main() -> int:
    from app.sandbox.manager import get_manager
    from app.workspace import user_workspace

    mgr = get_manager()
    print("backend =", getattr(mgr, "backend_name", "?"))
    print("mode    =", os.environ.get("HLZ_SANDBOX_MODE"))

    handled, note = await mgr.write_file(USER, REL, EXPECT, mode="overwrite")
    print(f"handled={handled} note={note!r}")

    target = user_workspace(USER, create=False) / REL
    if not target.is_file():
        print(f"FAIL: 文件没落盘 {target}")
        return 1
    got = target.read_text(encoding="utf-8")
    if got != EXPECT:
        print(f"FAIL: 内容不符\n got={got!r}\n want={EXPECT!r}")
        return 1
    print(f"OK: 落盘并内容一致 {target}")
    print(f"     size={target.stat().st_size}")

    # 逐级检查权限：每一层都必须让 uid 1000 可写
    import stat as statmod
    d = user_workspace(USER, create=False)
    parts = REL.split("/")[:-1]
    cur = d
    print("\n目录权限逐级检查：")
    ok = True
    for p in parts:
        cur = cur / p
        st = cur.stat()
        mode = st.st_mode
        g_w = bool(mode & statmod.S_IWGRP)
        sgid = bool(mode & statmod.S_ISGID)
        gid = st.st_gid
        flag = "OK " if (g_w and sgid and gid == 1000) else "BAD"
        if flag == "BAD":
            ok = False
        print(f"  {flag} {oct(mode & 0o7777)} gid={gid} {cur.relative_to(d)}")
    if not ok:
        print("FAIL: 有目录不可被组写")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))