"""反向确认：MCP 工具数口径一致性。

## 三个用例

1. 基线：PASS
2. **mcp_probe 退回只返回 MCP 工具**（去掉 build_builtin_tools）→ 必须红
3. **前端退回只显示总数**（删掉拆分渲染）→ 必须红

第 2 组是本次真机事故的原样复现：46 vs 35 的根本原因就是 probe 没并入内置工具。
"""
from __future__ import annotations

import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TEST = "backend/tests/test_mcp_tool_count_consistency.py"
ME = "backend/app/routers/me.py"
FE = "frontend/src/components/SettingsModal.tsx"


def run() -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def with_patch(path: str, old: str, new: str):
    orig = open(path, encoding="utf-8", newline="").read()
    patched = orig.replace(old, new, 1)
    if patched == orig:
        return None, f"注入串没匹配上：{old[:70]!r}"
    try:
        open(path, "w", encoding="utf-8", newline="\n").write(patched)
        rc, out = run()
    finally:
        open(path, "w", encoding="utf-8", newline="\n").write(orig)
    return rc, out


def main() -> int:
    rc, out = run()
    if rc != 0:
        print("✗ 基线不通过：")
        print(out[-700:])
        return 1
    print("[✓] 用例 1（基线）：PASS")

    # 用例 2：probe 退回「只有 MCP」——这正是 46→35 的原样
    old = '''        builtin = _builtin_names()
        return {"ok": True, "servers": servers,
                # 与 mcp-effective 同口径：内置 + MCP
                "tools": builtin + mcp_names,
                "builtin_count": len(builtin),
                "mcp_count": len(mcp_names)}'''
    new = '''        return {"ok": True, "servers": servers,
                "tools": mcp_names,
                "builtin_count": 0,
                "mcp_count": len(mcp_names)}'''
    rc2, out2 = with_patch(ME, old, new)
    if rc2 is None:
        print(f"✗ [probe 退回只给 MCP] {out2}")
        return 1
    if rc2 == 0:
        print("✗ [probe 退回只给 MCP] 测试仍全绿 ⇒ 这个事故没被测住")
        return 1
    print("[✓] 用例 2（probe 退回只给 MCP）：FAIL（复现了 46→35）")

    # 用例 3：前端退回只显示总数
    old3 = '''            {mcpState !== 'ready'
              ? '工具数待检测'
              : builtinCount !== null && mcpCount !== null
              ? `MCP ${mcpCount} 个 · 内置 ${builtinCount} 个（合计 ${effective.length}）`
              : `共 ${effective.length} 个可用工具`}'''
    new3 = '''            {mcpState === 'ready' ? `共 ${effective.length} 个可用工具` : '工具数待检测'}'''
    rc3, out3 = with_patch(FE, old3, new3)
    if rc3 is None:
        print(f"✗ [前端退回总数] {out3}")
        return 1
    if rc3 == 0:
        print("✗ [前端退回总数] 测试仍全绿 ⇒ 拆分显示没被测住")
        return 1
    print("[✓] 用例 3（前端退回只显示总数）：FAIL（符合预期）")

    rc4, _ = run()
    if rc4 != 0:
        print("✗ 恢复后仍失败")
        return 1
    print("[✓] 用例 4（恢复后）：重新 PASS")
    print("\n✓ 反向确认通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())