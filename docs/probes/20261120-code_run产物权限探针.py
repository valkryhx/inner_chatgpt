"""`code_run` 建出来的目录/文件，权限位对 app 侧是否够用？

## 为什么必须查这条

hx 的现场是：`file_write` 全线 507，于是**改用 python（= code_run）写入**，
文件"正常生成"了。所以那个报告的真实生产者是 code_run。

如果 code_run 建的目录是 0755（组不可写）、文件是 0644，那么下一次
app 的 `file_patch` 去改它就会失败 —— 只在「先 code_run 写、再 file_patch 改」
这个序列里复现。

实测（当前栈）：mkdir → 0o42755，write_text → 0o100644，组都不可写。
"""
import asyncio
import sys

sys.path.insert(0, "/app/backend")

USER = "u_c56b8337dd09"
REL_DIR = ".subagents/coderun_probe/research"
REL_FILE = REL_DIR + "/a.md"


async def main() -> int:
    import shutil

    from app.sandbox.manager import get_manager
    from app.workspace import user_workspace

    mgr = get_manager()
    base = user_workspace(USER, create=False)

    # ★ 先删掉上次的产物。
    #
    # 不删的话这条探针会**读到历史遗留文件**并给出错误结论 ——
    # 本探针实际踩过一次：改了 umask 之后仍然报 `DIR 0o2755 组可写=False`，
    # 看起来像"修复无效"，其实那个目录是上一轮（umask 还是 022 时）建的，
    # 而 `mkdir(exist_ok=True)` 不会重建已存在的目录。
    shutil.rmtree(base / REL_DIR, ignore_errors=True)

    code = (
        "import pathlib\n"
        f"d = pathlib.Path('/ws/{REL_DIR}')\n"
        "d.mkdir(parents=True, exist_ok=True)\n"
        "p = d / 'a.md'\n"
        "p.write_text('# code_run 写入\\n')\n"
        "print('DIR', oct(d.stat().st_mode & 0o7777), d.stat().st_gid)\n"
        "print('FILE', oct(p.stat().st_mode & 0o7777), p.stat().st_gid)\n"
    )
    handled, out, marker = await mgr.exec_code(USER, code, timeout=30)
    print(f"handled={handled} marker={marker!r}")
    print(out)

    d = base / REL_DIR
    f = base / REL_FILE
    ok = True
    for label, p in (("dir", d), ("file", f)):
        if not p.exists():
            print(f"{label} 不存在: {p}")
            ok = False
            continue
        st = p.stat()
        group_writable = bool(st.st_mode & 0o020)
        print(f"{label} 实际: mode={oct(st.st_mode & 0o7777)} gid={st.st_gid} "
              f"组可写={group_writable}")
        if not group_writable:
            ok = False
    print("PASS" if ok else "FAIL: code_run 产物组不可写 → 之后 file_patch 会 507")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))