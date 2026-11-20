"""反向确认：给 work/_verify_sqlbot_doc.py 注入错数字，必须变红。"""
import subprocess, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DOC = "docs/20261120-多智能体读写慢的定量排查.md"
SCRIPT = "work/_verify_sqlbot_doc.py"

def run():
    r = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode

orig = open(DOC, encoding="utf-8", newline="").read()
if run() != 0:
    print("✗ 基线不通过"); sys.exit(1)
print("[✓] 用例 1（基线）：PASS")

bad = orig.replace("40,817", "48,999")
if bad == orig:
    print("✗ 注入失败——字符串没匹配上"); sys.exit(1)
open(DOC, "w", encoding="utf-8", newline="\n").write(bad)
try:
    rc = run()
finally:
    open(DOC, "w", encoding="utf-8", newline="\n").write(orig)
if rc == 0:
    print("✗ 注入错数字后仍全绿 ⇒ 核验无效"); sys.exit(1)
print("[✓] 用例 2（注入 48,999）：FAIL（符合预期）")

if run() != 0:
    print("✗ 恢复后仍失败"); sys.exit(1)
print("[✓] 已恢复，基线重新 PASS")
print("\n✓ 反向确认通过")
