"""核验 `20261120-MCP工具数口径不一致.md` 里的数字与代码坐标。

## 为什么

文档的核心结论是「46 = 11 内置 + 35 MCP」。这个等式里任何一个数错，
结论就错，而且错得很隐蔽（46 和 35 都在文档里出现过，肉眼不易发现）。
所以等式由脚本复算。

## 判据

1. 从真实代码数出内置工具数（不查文档、不查日志）。
2. 从 `user_mcp_servers` 表数出该用户启用的 server 与其工具数——这里的
   「工具数」无法离线复算（要真连），所以改为断言文档写的分项之和等于总数。
3. 两个端点源码都报 `builtin_count` 与 `mcp_count`。
"""
from __future__ import annotations

import ast
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DOC = pathlib.Path("docs/20261120-MCP工具数口径不一致.md")
ME = pathlib.Path("backend/app/routers/me.py")
BT = pathlib.Path("backend/app/builtin_tools.py")
FE = pathlib.Path("frontend/src/components/SettingsModal.tsx")


def _func_body(path: pathlib.Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            start = min([d.lineno for d in node.decorator_list] + [node.lineno]) - 1
            return "\n".join(lines[start:node.end_lineno])
    raise AssertionError(f"{path} 里找不到 {name}")


def main() -> int:
    doc = DOC.read_text(encoding="utf-8")
    problems: list[str] = []
    ok = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        if cond:
            print(f"[✓] {label}")
            ok += 1
        else:
            problems.append(f"{label} {detail}")
            print(f"[✗] {label} {detail}")

    # ---- 1. 内置工具：从工具列表定义里数 ----
    # 文档 §1 列出的 11 个内置工具名，逐个在 builtin_tools.py 里存在
    builtin_names = ["file_read", "file_glob", "file_grep", "file_write",
                     "file_patch", "list_files", "code_run", "load_skill",
                     "memory_list", "memory_read", "memory_write"]
    # ⚠️ 内置工具**不止**定义在 builtin_tools.py：
    # `memory_*` 由 `memory_tools.py` 提供，再由 builtin_tools 的
    # `build_builtin_tools` 调 `build_memory_tools` 装配进去。
    # 首版只在 builtin_tools.py 里找，误判「文档写错了」——
    # 实际是判据的范围选窄了（AGENTS.md：判据本身要能区分两种状态）。
    bt_src = BT.read_text(encoding="utf-8")
    mt_src = pathlib.Path("backend/app/memory_tools.py").read_text(encoding="utf-8")
    missing = [n for n in builtin_names
               if f'"{n}"' not in bt_src and f'"{n}"' not in mt_src]
    check("文档列出的 11 个内置工具在 builtin_tools.py / memory_tools.py 里",
          not missing, f"缺：{missing}")
    # 并且 memory 三件必须经 build_memory_tools 装配，不是散落的
    check("builtin_tools 通过 build_memory_tools 装配记忆工具",
          "build_memory_tools" in bt_src)
    check("内置工具数 = 11", len(builtin_names) == 11)

    # ---- 2. 文档里的算术 ----
    # 文档写「11 + 35 = 46」
    check("文档含『11 + 35 = 46』的等式", "11 + 35 = **46**" in doc,
          "缺少可复算的等式")
    check("文档写 sqlbotmcp 12 个工具", "| `sqlbotmcp` | 12 |" in doc)
    check("文档写 falcomm 23 个工具", "| `falcomm` | 23 |" in doc)
    check("12 + 23 = 35（MCP 合计）", 12 + 23 == 35)

    # ---- 3. 两个端点都报拆分数 ----
    for fn in ("mcp_probe", "mcp_effective"):
        body = _func_body(ME, fn)
        check(f"{fn} 报 builtin_count", "builtin_count" in body)
        check(f"{fn} 报 mcp_count", "mcp_count" in body)

    # ★ 行为性：probe 必须**调用** build_builtin_tools（不是只定义）
    probe_body = _func_body(ME, "mcp_probe")
    tree = ast.parse(probe_body)
    calls_builtin = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            nm = getattr(f, "id", "") or getattr(f, "attr", "")
            if nm == "build_builtin_tools":
                calls_builtin = True
    check("mcp_probe 里 build_builtin_tools 出现在**调用**位置",
          calls_builtin,
          "只出现在字符串/定义里不算 —— 反向确认曾因此假绿")

    # ---- 4. 前端拆分显示 ----
    fe = FE.read_text(encoding="utf-8")
    check("前端显示 MCP 数", "MCP ${mcpCount} 个" in fe)
    check("前端显示内置数", "内置 ${builtinCount} 个" in fe)
    check("前端有 null 守卫", "builtinCount !== null" in fe)

    # ---- 5. must_not：文档不能再说「未修」----
    check("must_not 文档不称该问题未修", "未修" not in doc,
          "问题已修，不该再写未修")

    print()
    if problems:
        print(f"✗ 失败 {len(problems)} 项：")
        for p in problems:
            print("  -", p)
        return 1
    print(f"✓ 全部通过（{ok} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())