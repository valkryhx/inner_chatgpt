"""反向确认：MCP 工具数口径核验脚本会因注入的错误而变红。"""
import subprocess, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DOC = "docs/20261120-MCP工具数口径不一致.md"
SCRIPT = "docs/probes/20261120-MCP工具数口径核验.py"

def run():
    r = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode

orig = open(DOC, encoding="utf-8", newline="").read()
if run() != 0:
    print("✗ 基线不通过"); sys.exit(1)
print("[✓] 用例 1（基线）：PASS")

# 注入 1：把等式改错
bad = orig.replace("11 + 35 = **46**", "11 + 35 = **99**")
if bad == orig:
    print("✗ 注入失败"); sys.exit(1)
try:
    open(DOC, "w", encoding="utf-8", newline="\n").write(bad)
    rc = run()
finally:
    open(DOC, "w", encoding="utf-8", newline="\n").write(orig)
if rc == 0:
    print("✗ [错等式] 仍全绿"); sys.exit(1)
print("[✓] 用例 2（等式改错）：FAIL（符合预期）")

# 注入 2：把 sqlbotmcp 的工具数改错
bad2 = orig.replace("| `sqlbotmcp` | 12 |", "| `sqlbotmcp` | 99 |")
if bad2 == orig:
    print("✗ 第二个注入失败"); sys.exit(1)
try:
    open(DOC, "w", encoding="utf-8", newline="\n").write(bad2)
    rc2 = run()
finally:
    open(DOC, "w", encoding="utf-8", newline="\n").write(orig)
if rc2 == 0:
    print("✗ [错 server 工具数] 仍全绿"); sys.exit(1)
print("[✓] 用例 3（server 工具数改错）：FAIL（符合预期）")

if run() != 0:
    print("✗ 恢复后仍失败"); sys.exit(1)
print("[✓] 用例 4（恢复后）：重新 PASS")
print("\n✓ 反向确认通过")
