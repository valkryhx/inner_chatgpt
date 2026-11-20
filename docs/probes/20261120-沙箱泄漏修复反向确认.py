"""反向确认（两条独立判据都要能变红）。

1. 撤掉 conftest 的销毁调用 → conftest 守卫 + 行为测试必须变红
2. 撤掉 leak_guard 的超时 → 挂死守卫必须变红（可能表现为超时/失败）
"""
import pathlib, subprocess, sys, shutil
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CF = pathlib.Path("backend/tests/conftest.py")
LG = pathlib.Path("backend/app/sandbox/leak_guard.py")
TEST = "backend/tests/test_sandbox_pool_leak_in_tests.py"
fails = []

def run():
    r = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    return r.returncode, r.stdout + r.stderr

# ---------- 反向 A：撤掉 conftest 销毁 ----------
print("== 反向 A：撤掉 conftest 的销毁调用 ==")
cf_src = CF.read_text(encoding="utf-8")
shutil.copy2(CF, "work/_cf_bak.py")
broken = cf_src.replace(
    """                from app.sandbox import leak_guard
                leak_guard.release_pool_sync(
                    sbx_manager.get_pool(), timeout_s=30.0)""",
    """                pass  # 故意撤掉""")
assert broken != cf_src, "A 没替换成功"
CF.write_text(broken, encoding="utf-8", newline="\n")
try:
    rc, out = run()
    red = rc != 0 and "test_conftest_teardown_calls_pool_shutdown" in out
    print(f"   returncode={rc} 目标用例变红={red}")
    if not red: fails.append("A: 撤掉 conftest 销毁后测试没变红")
finally:
    CF.write_text(cf_src, encoding="utf-8", newline="\n")

# ---------- 反向 B：撤掉 leak_guard 的超时 ----------
print("\n== 反向 B：撤掉 leak_guard 的超时 ==")
lg_src = LG.read_text(encoding="utf-8")
shutil.copy2(LG, "work/_lg_bak.py")
# 把 wait_for 换成裸 await —— 正是「会挂死」的写法
naked = lg_src.replace(
    "n = await asyncio.wait_for(pool.shutdown(), timeout=timeout_s)",
    "n = await pool.shutdown()  # 裸 await，无超时")
assert naked != lg_src, "B 没替换成功"
LG.write_text(naked, encoding="utf-8", newline="\n")
try:
    try:
        rc, out = run()
        red = rc != 0 and "test_teardown_shutdown_does_not_hang" in out
        print(f"   returncode={rc} 目标用例变红={red}")
        if not red: fails.append("B: 撤掉超时后挂死守卫没变红")
    except subprocess.TimeoutExpired:
        print("   ✓ 整个测试进程挂死超时（180s）—— 正是要防的后果")
finally:
    LG.write_text(lg_src, encoding="utf-8", newline="\n")

print("\n== 还原后确认全绿 ==")
rc, out = run()
print(f"   returncode={rc}")
if rc != 0:
    fails.append(f"还原后仍有失败：{out[-400:]}")

print()
if fails:
    print("✗ 反向确认失败：")
    for f in fails: print("  -", f)
    sys.exit(1)
print("✓ 反向确认通过：两条判据各自能变红，还原后全绿")
