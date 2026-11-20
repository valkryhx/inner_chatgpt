"""真机验证：默认 2000 行 + 截断横幅带翻页指引。

## 为什么在容器里跑

`DEFAULT_READ_LINES` 是模块级常量，本地（Windows）能测。
但截断横幅里用了 `display_path`（依赖 `WORKSPACES_DIR` 与沙箱路径解析），
真机跑一遍能确认两边文案一致、且中文不乱码。
"""
import asyncio
import sys

sys.path.insert(0, "/app/backend")

USER = "u_c56b8337dd09"


async def main() -> int:
    from app.builtin_tools import DEFAULT_READ_LINES, build_builtin_tools

    print(f"DEFAULT_READ_LINES = {DEFAULT_READ_LINES}")
    if DEFAULT_READ_LINES != 2000:
        print("  FAIL: 不是 2000")
        return 1

    tools = {t.name: t for t in build_builtin_tools(user_id=USER)}
    path = ".subagents/read_probe/big.md"

    # 1500 行：应一次读完
    big = "\n".join(f"line {i}" for i in range(1500)) + "\n"
    await tools["file_write"].call({"path": path, "content": big})
    out = await tools["file_read"].call({"path": path})
    first = out.splitlines()[0]
    print(f"[1500 行] {first}")
    if "显示 1-1500" not in first or "Truncated" in out:
        print("  FAIL: 1500 行没有一次读完")
        return 1

    # 2500 行：应截断 + 给翻页参数
    huge = "\n".join(f"line {i}" for i in range(2500)) + "\n"
    await tools["file_write"].call({"path": path, "content": huge})
    out2 = await tools["file_read"].call({"path": path})
    print(f"[2500 行] 头部: {out2.splitlines()[0]}")
    tail = out2[out2.index("[Truncated"):]
    print("[2500 行] 横幅:")
    for line in tail.splitlines():
        print("   ", line)
    if "start=2001" not in out2:
        print("  FAIL: 横幅没给下一页参数")
        return 1
    if "不要在只看这一页" not in out2:
        print("  FAIL: 横幅缺告警")
        return 1

    # 按横幅指示翻到最后一页
    out3 = await tools["file_read"].call({"path": path, "start": 2001})
    print(f"[翻页] {out3.splitlines()[0]}")
    if "显示 2001-2500" not in out3 or "Truncated" in out3:
        print("  FAIL: 翻页结果不对")
        return 1

    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))