"""反向确认：侧边栏「MCP 46 工具」这个问题是否被测住。

用例：
1. 基线 PASS
2. App.tsx 退回 `mcp_tools: eff.tools.length`（这是 46 的来源）→ 必须红
3. Sidebar 退回只显示一个总数 → 必须红
"""
import subprocess, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TEST = "backend/tests/test_mcp_tool_count_consistency.py"
APP = "frontend/src/App.tsx"
SIDE = "frontend/src/components/Sidebar.tsx"

def run():
    r = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")

def with_patch(path, old, new):
    orig = open(path, encoding="utf-8", newline="").read()
    patched = orig.replace(old, new, 1)
    if patched == orig:
        return None, f"注入串没匹配上：{old[:60]!r}"
    try:
        open(path, "w", encoding="utf-8", newline="\n").write(patched)
        rc, out = run()
    finally:
        open(path, "w", encoding="utf-8", newline="\n").write(orig)
    return rc, out

rc, out = run()
if rc != 0:
    print("✗ 基线不通过"); print(out[-600:]); sys.exit(1)
print("[✓] 用例 1（基线）：PASS")

rc2, out2 = with_patch(APP, "      mcp_tools: mcpCount,", "      mcp_tools: eff ? eff.tools.length : 0,")
if rc2 is None:
    print(f"✗ {out2}"); sys.exit(1)
if rc2 == 0:
    print("✗ [App 退回总数] 仍全绿 ⇒ 没测住"); sys.exit(1)
print("[✓] 用例 2（App 退回 tools.length）：FAIL（复现 46）")

old_side = """            : health?.builtin_tools
            ? `MCP ${health?.mcp_tools ?? 0} · 内置 ${health.builtin_tools}`
            : `MCP 正常 · ${health?.mcp_tools ?? 0} 工具`}"""
new_side = """            : `MCP 正常 · ${(health?.mcp_tools ?? 0) + (health?.builtin_tools ?? 0)} 工具`}"""
rc3, out3 = with_patch(SIDE, old_side, new_side)
if rc3 is None:
    print(f"✗ {out3}"); sys.exit(1)
if rc3 == 0:
    print("✗ [Sidebar 退回总数] 仍全绿 ⇒ 没测住"); sys.exit(1)
print("[✓] 用例 3（Sidebar 退回总数）：FAIL（符合预期）")

if run()[0] != 0:
    print("✗ 恢复后仍失败"); sys.exit(1)
print("[✓] 用例 4（恢复后）：重新 PASS")
print("\n✓ 反向确认通过")
