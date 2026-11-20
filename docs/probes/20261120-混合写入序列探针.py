"""hx 的完整现场复现：file_write 507 → 改用 code_run 写 → 之后再 file_patch。

## 三件事一起验

1. `file_write` 写进遗留（755 root:root）目录 → 必须成功（修复前是 507）
2. `code_run` 在该目录里建文件 → 产物必须组可写（修复前是 0644）
3. `file_patch` 改 code_run 建的那个文件 → 必须成功（修复前会 507）

第 3 步是真正把前两步串起来的那条 —— 它同时依赖「目录组可写」与
「文件组可写」。只测 1、2 任一条都会漏掉另一半。
"""
import asyncio
import os
import sys

sys.path.insert(0, "/app/backend")

USER = "u_c56b8337dd09"
SESSION = "co_hybrid_probe"


async def main() -> int:
    from app.sandbox.manager import get_manager
    from app.workspace import user_workspace

    mgr = get_manager()
    base = user_workspace(USER, create=False)
    ok = True

    # ---- 1) 造一个遗留 65534 / 755 的目录，file_write 必须能修好并写进去 ----
    legacy = base / ".subagents" / SESSION / "research"
    legacy.mkdir(parents=True, exist_ok=True)
    os.chown(legacy, 65534, 65534)
    os.chmod(legacy, 0o755)
    print(f"[1] 遗留目录 uid={legacy.stat().st_uid} "
          f"mode={oct(legacy.stat().st_mode & 0o7777)}")

    rel1 = f".subagents/{SESSION}/research/from_file_write.md"
    handled, note = await mgr.write_file(USER, rel1, "# file_write\n", mode="overwrite")
    print(f"[1] write_file handled={handled} note={note!r}")
    if not (base / rel1).is_file():
        print("    FAIL: 没落盘")
        ok = False

    # ---- 2) code_run 在同一目录里写第二个文件 ----
    rel2_name = "from_code_run.md"
    code = (
        "import pathlib\n"
        f"p = pathlib.Path('/ws/.subagents/{SESSION}/research/{rel2_name}')\n"
        "p.write_text('# code_run\\n')\n"
        "print('MODE', oct(p.stat().st_mode & 0o7777))\n"
    )
    handled2, out2, _ = await mgr.exec_code(USER, code, timeout=30)
    print(f"[2] exec_code handled={handled2} out={out2.strip()!r}")

    f2 = base / ".subagents" / SESSION / "research" / rel2_name
    st2 = f2.stat()
    gw2 = bool(st2.st_mode & 0o020)
    print(f"[2] 产物 mode={oct(st2.st_mode & 0o7777)} 组可写={gw2}")
    if not gw2:
        print("    FAIL: code_run 产物组不可写")
        ok = False

    # ---- 3) file_patch 改 code_run 建的那个文件 ----
    rel2 = f".subagents/{SESSION}/research/{rel2_name}"
    handled3, note3 = await mgr.patch_file(
        USER, rel2, "# code_run\n", "# code_run（已被 file_patch 改过）\n",
    )
    print(f"[3] patch_file handled={handled3} note={note3!r}")
    # 注意断言串必须与 replace 的 new 一致：第一次写成 "# 已被 file_patch 改过"，
    # 而实际内容是 "# code_run（已被 file_patch 改过）" —— 探针自己判错，
    # 报了个假的 FAIL。断言串直接复用变量，别再手抄。
    want = "# code_run（已被 file_patch 改过）\n"
    got = f2.read_text(encoding="utf-8")
    if got != want:
        print(f"    FAIL: 内容不符 got={got!r} want={want!r}")
        ok = False

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))