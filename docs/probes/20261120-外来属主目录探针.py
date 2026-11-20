"""复现 hx 报的场景：目录属主是 65534（unmapped uid），沙箱 uid 1000 写不进去。

## 为什么用 65534 而不是随便一个 uid

65534 = `nobody`，也是 Linux 的 **overflow uid**：当 bwrap 的用户命名空间
里某个 uid 没有被映射到宿主时，宿主看到的就是 65534。
hx 报的 `/ws/research 属主为 65534` 就是这种「映射没建立」的产物。

这条探针不追究 65534 怎么来的（各环境不同），只回答一个问题：
**修复后的代码能不能把一个外来属主的目录修好。**
"""
import asyncio
import os
import sys

sys.path.insert(0, "/app/backend")

USER = "u_c56b8337dd09"
REL = ".subagents/legacy65534/research/findings.md"
EXPECT = "# 65534 目录写入验证\n"

LEGACY = ".subagents/legacy65534/research"


async def main() -> int:
    from app.sandbox.manager import get_manager
    from app.workspace import user_workspace

    base = user_workspace(USER, create=False)
    legacy = base / LEGACY
    legacy.mkdir(parents=True, exist_ok=True)

    # 造出 65534 属主 + 755（组不可写）—— 正是 hx 看到的状态
    os.chown(legacy, 65534, 65534)
    os.chmod(legacy, 0o755)
    st = legacy.stat()
    print(f"造出遗留目录: uid={st.st_uid} gid={st.st_gid} mode={oct(st.st_mode & 0o7777)}")

    mgr = get_manager()
    handled, note = await mgr.write_file(USER, REL, EXPECT, mode="overwrite")
    print(f"handled={handled} note={note!r}")

    target = base / REL
    if not target.is_file():
        print(f"FAIL: 文件没落盘")
        return 1
    if target.read_text(encoding="utf-8") != EXPECT:
        print("FAIL: 内容不符")
        return 1

    st2 = legacy.stat()
    print(f"修复后:       uid={st2.st_uid} gid={st2.st_gid} mode={oct(st2.st_mode & 0o7777)}")
    ok = (st2.st_gid == 1000) and (st2.st_mode & 0o2000) and (st2.st_mode & 0o020)
    print("PASS" if ok else "FAIL: 目录没被修成 2775/gid1000")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))