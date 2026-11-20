"""真机验证：门禁删除后，「读不全 → 覆盖写」这条路径通了。

## 复现 writer_c 卡住的那一步

1. 先 file_write 建一个大文件（> 200 行 = DEFAULT_READ_LINES）
2. 只 file_read 前几行（模拟「只读了片段」）
3. 再 file_write 覆盖它 —— 旧代码在这里被拒

## 判据

第 3 步必须成功。修复前它会返回「只读了片段，请读完整文件后再写」。
"""
import asyncio
import sys

sys.path.insert(0, "/app/backend")

USER = "u_c56b8337dd09"


async def main() -> int:
    from app.builtin_tools import build_builtin_tools

    tools = {t.name: t for t in build_builtin_tools(user_id=USER)}
    path = ".subagents/gate_probe/big_report.md"

    big = "\n".join(f"line {i}" for i in range(1000)) + "\n"
    out1 = await tools["file_write"].call({"path": path, "content": big})
    print(f"[1] 建大文件: {out1[:120]}")
    if "已写入" not in out1:
        print("    FAIL: 第 1 步就没成")
        return 1

    out2 = await tools["file_read"].call({"path": path, "count": 20})
    print(f"[2] 只读 20 行（共 1000 行）: {out2.splitlines()[0][:100]}")

    out3 = await tools["file_write"].call(
        {"path": path, "content": "# 覆盖后的新报告\n"})
    print(f"[3] 覆盖写: {out3[:140]}")
    if "已写入" not in out3:
        print(f"    FAIL: 仍被门禁拦住 —— {out3}")
        return 1

    # 再验 patch 也不需要先读
    out4 = await tools["file_patch"].call({
        "path": path,
        "old_content": "覆盖后的新报告",
        "new_content": "覆盖后的新报告（已 patch）",
    })
    print(f"[4] 不读直接 patch: {out4[:140]}")
    # ⚠️ 文案两侧不一致：**进程内**回落路径是「已局部修改」，
    # **沙箱服务**路径是「已修改」。这不是缺陷（两条路径早就各写各的），
    # 但探针断言必须两边都认，否则会像上一版那样报假 FAIL。
    # 这也是本轮第三次栽在「手抄成功文案」上。
    if not ("已修" in out4):
        print(f"    FAIL: patch 仍被拦 —— {out4}")
        return 1

    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))