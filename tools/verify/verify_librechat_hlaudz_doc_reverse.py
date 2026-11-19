"""反向确认：证明 verify_librechat_hlaudz_doc.py 的断言引擎**会变红**。

## 为什么必须有这一步（AGENTS.md §8.5 / §8.6）

全绿不携带信息 —— 如果引擎本身写错了（比如永远返回 PASS），全量跑也是全绿。
所以注入错误断言，要求引擎报 FAIL；同时注入一个**正确**断言，要求它报 PASS。
只有「该红的红、该绿的绿」才证明引擎有效。

## 注入方式

不修改被测脚本，而是在内存里 import 它、覆盖它的常量表，再独立调各 check 函数。
所以被测脚本必须是「常量表 + 纯函数」结构，这一点在写它时就按此约束了。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "verify_librechat_hlaudz_doc.py"

spec = importlib.util.spec_from_file_location("verifier", TARGET)
assert spec and spec.loader, f"加载不了 {TARGET}"
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

LC = mod.LIBRECHAT
HZ = mod.HLAUDZ
RJ = LC / "packages/api/src/stream/implementations/RedisEventTransport.ts"
TJ = HZ / "backend/app/turn_job.py"
NG = HZ / "backend/deploy/nginx.conf"

results: list[tuple[str, bool, str]] = []  # (名字, 是否通过, 说明)


def run(name: str, cases: list, expect_fail: bool, why: str) -> None:
    """临时替换 CASES 跑一次，检查是否符合预期。"""
    saved = mod.CASES
    try:
        mod.CASES = cases
        fails = mod.check_lines()
    finally:
        mod.CASES = saved
    got_fail = bool(fails)
    ok = (got_fail == expect_fail)
    detail = "" if ok else f"期望 {'FAIL' if expect_fail else 'PASS'}，实际 {'FAIL' if got_fail else 'PASS'}"
    if fails:
        detail += f" | 第一条: {fails[0].splitlines()[0] if fails else ''}"
    results.append((f"{name}（{why}）", ok, detail))


# ============================================================ 必须变红
run("行号写错", [(  "错误行号", RJ, 1, "const CHANNELS = {")], True,
    "行号 1 不是 CHANNELS")

run("片段写错", [(  "错误片段", RJ, 24, "const CHANNELS_XX = {")], True,
    "片段不存在")

run("文件不存在", [(  "缺文件", LC / "no_such_file.ts", 1, "x")], True,
    "文件不存在必须报错而不是静默跳过")


# ==================================================== 必须通过（正向对照）
run("正确断言", [(  "正确", RJ, 24, "const CHANNELS = {")], False,
    "引擎在正确输入下必须 PASS（否则只会全红，也不携带信息)")

run("中文字符串断言", [(  "中文", NG, 91, "hash $cookie_hlz_sticky;")], False,
    "中文/特殊字符路径也要能 PASS")


# ============================================================ must_not 反向
def check_must_not_injection(name: str, target: Path, injected: str, expect_hit: bool) -> None:
    """往一个临时副本里塞禁忌词，看 check_must_not 是否发现。

    不改真文件：复制到内存字符串层面做不到（函数读盘），所以用一座临时目录
    + 重定向 mod.LIBRECHAT / mod.HLAUDZ 到它。
    """
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="revconf_"))
    try:
        # 造最小目录结构
        lc_stream = tmp / "lc/packages/api/src/stream"
        lc_stream.mkdir(parents=True)
        (tmp / "lc/package.json").write_text("{}", encoding="utf-8")
        sample = lc_stream / "Sample.ts"
        sample.write_text(f"// {injected}\nconst x = 1;\n", encoding="utf-8")

        hz = tmp / "hz/backend/app"
        hz.mkdir(parents=True)
        (hz / "turn_job.py").write_text("x = 1\n", encoding="utf-8")

        saved_lc, saved_hz = mod.LIBRECHAT, mod.HLAUDZ
        try:
            mod.LIBRECHAT = tmp / "lc"
            mod.HLAUDZ = tmp / "hz"
            fails = mod.check_must_not()
        finally:
            mod.LIBRECHAT, mod.HLAUDZ = saved_lc, saved_hz

        got = bool([f for f in fails if "stream 层出现了" in f])
        results.append((f"{name}", got == expect_hit,
                        "" if got == expect_hit else f"期望检出={expect_hit}，实际={got}"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


check_must_not_injection("must_not 能抓到 sticky", None, "sticky session", True)
check_must_not_injection("must_not 不误伤普通注释", None, "this is a normal comment", False)


# ============================================================ 汇总
print("=" * 70)
bad = 0
for name, ok, detail in results:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  <- {detail}" if detail else ""))
    if not ok:
        bad += 1
print("=" * 70)
print(f"共 {len(results)} 条，符合预期 {len(results) - bad}，不符合 {bad}")

pos = [r for r in results if "正确断言" in r[0] or "不误伤" in r[0]]
if not pos:
    print("!! 缺正向对照，这次反向确认不成立")
    sys.exit(2)

if bad:
    print("反向确认失败：断言引擎的行为与预期不符")
    sys.exit(1)
print("反向确认通过：该红的红了，该绿的绿了")
sys.exit(0)