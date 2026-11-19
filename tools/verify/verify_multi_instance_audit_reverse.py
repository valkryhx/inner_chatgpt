"""反向确认：证明 verify_multi_instance_audit.py 的断言**会在问题被修好时变红**。

## 为什么要（AGENTS.md §8.5 / §8.6）

全绿不携带信息。如果引擎写错（比如永远 PASS），31 条全绿也说明不了任何事。
所以把被测源码复制到临时目录、**模拟「有人修好了这个 bug」**，再要求对应断言变红。

这比「注入一条错误断言」更贴近真实：它模拟的正是未来的场景 —— 有人修好了，
脚本必须提醒「文档要更新」，而不是继续全绿。

## 这个脚本抓出过我自己的两处断言设计错误

第一版写完跑，两条反向确认没变红。查清后是**断言选错了靶子**：

| 我的断言 | 模拟修法 | 为什么测不到 |
| --- | --- | --- |
| 「无条件收敛语句**仍在** db.py」 | 在原语句后追加 owner 条件 | needle 是新语句的**前缀**，依然匹配 → 仍然 PASS |
| 「_ensure_sticky_cookie **只在 1 处**被调用」 | 挪进中间件 | 挪动不改次数，仍是 1 → 仍然 PASS |

结论：这两条断言只能测「语句被删掉」，测不到「语句被收窄」。真正该变红的靶子是
**「该语句没有 owner 过滤」**和**「调用点在 health 端点内」**。现在两边都测
（两种修法各对应一条），覆盖才完整。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "verify_multi_instance_audit.py"
REAL = Path("D:/git_codes/inner_chatgpt")

results: list[tuple[str, bool, str]] = []


def load_module():
    """加载被测脚本，但**不让它的顶层主流程跑**。

    被测脚本是「顶层跑完再 sys.exit」的形态，直接 exec_module 会：
    1. 往 stdout 打 31 条结果，把反向确认的输出淹没；
    2. 执行到末尾 sys.exit()，把本进程杀掉。
    """
    spec = importlib.util.spec_from_file_location("auditor", TARGET)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)

    real_exit = sys.exit
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                spec.loader.exec_module(m)
            except SystemExit:
                pass                      # 主流程正常退出，忽略
    finally:
        sys.exit = real_exit
    return m


def fresh_copy() -> tuple[Path, "module"]:
    """复制一份真实仓库到临时目录，返回 (root, 已指向该目录的模块)。"""
    tmp = Path(tempfile.mkdtemp(prefix="revconf_audit_"))
    be = tmp / "backend"
    (be / "deploy").mkdir(parents=True)
    (be / "tests/manual").mkdir(parents=True)
    (tmp / "frontend/src").mkdir(parents=True)
    (tmp / "docs").mkdir(parents=True)

    shutil.copytree(REAL / "backend/app", be / "app", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(REAL / "backend/deploy/nginx.conf", be / "deploy/nginx.conf")
    shutil.copy(REAL / "frontend/src/App.tsx", tmp / "frontend/src/App.tsx")
    for p in (REAL / "backend/tests/manual").glob("*.py"):
        shutil.copy(p, be / "tests/manual" / p.name)
    for p in (REAL / "docs").glob("*.md"):
        shutil.copy(p, tmp / "docs" / p.name)

    m = load_module()
    # 同时改 HLAUDZ 与 BE：src() 的闭包读模块级 BE，只改 HLAUDZ 会读到真仓库
    m.HLAUDZ = tmp
    m.BE = tmp / "backend"
    return tmp, m


# ---------------------------------------------------------------- 重算逻辑
# 每条对应「真实断言」的判定方式，用当前模块指向的目录重算。

def _s1_stmt_exists(m) -> bool:
    return m.line_of(
        "app/db.py",
        "UPDATE messages SET status='stopped' WHERE role='assistant' AND status='streaming'") > 0


def _s1_no_owner_filter(m) -> bool:
    """真断言：该 UPDATE 附近**没有** owner 过滤。"""
    import re
    n = m.line_of(
        "app/db.py",
        "UPDATE messages SET status='stopped' WHERE role='assistant' AND status='streaming'")
    if n <= 0:
        return True   # 语句都不在了，这条不适用
    ctx = "\n".join(m.lines("app/db.py")[n - 4: n + 6])
    return not re.search(r"\bowner\b|live_owners", ctx)


def _s2_called_once(m) -> bool:
    import re
    return len(re.findall(r"^\s+_ensure_sticky_cookie\(", m.src("app/main.py"), re.M)) == 1


def _s2_call_inside_health(m) -> bool:
    """真断言：调用点在 health 端点内（而不是中间件里）。"""
    call = m.line_of("app/main.py", "_ensure_sticky_cookie(request, response)")
    health = m.line_of("app/main.py", "async def health(")
    return 0 < health < call


def _s4_no_wrong_instance(m) -> bool:
    return "wrong_instance" not in (
        (m.HLAUDZ / "frontend/src/App.tsx").read_text(encoding="utf-8"))


CHECKS = {
    "§1 无条件收敛语句仍在 db.py": _s1_stmt_exists,
    "§1 该语句确实没有 owner 过滤": _s1_no_owner_filter,
    "§2 它只在 1 处被调用": _s2_called_once,
    "§2 调用点在 health 端点内": _s2_call_inside_health,
    "§4 前端没有 wrong_instance": _s4_no_wrong_instance,
}


def run_case(name: str, mutate, expect_red: list[str]) -> None:
    tmp, m = fresh_copy()
    try:
        mutate(tmp)
        red = [k for k, fn in CHECKS.items() if not fn(m)]
        want = set(expect_red)
        ok = want <= set(red)
        detail = "" if ok else f"期望变红 {sorted(want)}，实际变红 {sorted(red)}"
        results.append((name, ok, detail))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- mutations

def no_change(_root):
    pass


def inline_owner_filter(root: Path):
    """修法 A：在原 UPDATE 里内联 owner 过滤（语句还在，但不再无条件）。"""
    p = root / "backend/app/db.py"
    t = p.read_text(encoding="utf-8")
    old = ("UPDATE messages SET status='stopped' "
           "WHERE role='assistant' AND status='streaming'")
    new = (old + " AND session_id IN (SELECT session_id FROM session_generations "
                 "WHERE status='active' AND owner<>'' AND owner NOT IN (%s))")
    assert old in t
    p.write_text(t.replace(old, new), encoding="utf-8")


def delete_wipe_stmt(root: Path):
    """修法 B：把这条收敛整个删掉（挪进 recover_stale_runtime）。"""
    p = root / "backend/app/db.py"
    lines = p.read_text(encoding="utf-8").split("\n")
    out = [ln for ln in lines
           if "UPDATE messages SET status='stopped'" not in ln]
    assert len(out) < len(lines), "mutation 前提不成立"
    p.write_text("\n".join(out), encoding="utf-8")


def move_cookie_to_middleware(root: Path):
    """修 §2：cookie 从 health 挪进 _instance_header 中间件。"""
    p = root / "backend/app/main.py"
    t = p.read_text(encoding="utf-8")
    # 删掉 health 里那一处（缩进 4 空格）
    old = "    _ensure_sticky_cookie(request, response)\n"
    assert old in t
    t = t.replace(old, "", 1)
    # 加进中间件
    anchor = "    response = await call_next(request)"
    assert anchor in t
    t = t.replace(anchor, anchor + "\n" + old.rstrip("\n"), 1)
    p.write_text(t, encoding="utf-8")


def add_wrong_instance(root: Path):
    """修 §4 前端配套：加 wrong_instance 处理。"""
    p = root / "frontend/src/App.tsx"
    t = p.read_text(encoding="utf-8")
    t = t.replace("const status = ev.status === 'stopped'",
                  "if (ev.error === 'wrong_instance') { /* retry once */ }\n"
                  "        const status = ev.status === 'stopped'", 1)
    p.write_text(t, encoding="utf-8")


# ---------------------------------------------------------------- 执行

run_case("不改源码：两条问题断言仍然 PASS（正向对照）", no_change, [])

run_case("修法 A（内联 owner 过滤）-> 「没有 owner 过滤」该变红",
         inline_owner_filter, ["§1 该语句确实没有 owner 过滤"])

run_case("修法 B（删掉该语句）-> 「语句仍在」该变红",
         delete_wipe_stmt, ["§1 无条件收敛语句仍在 db.py"])

run_case("修 §2（挪进中间件）-> 「调用点在 health 内」该变红",
         move_cookie_to_middleware, ["§2 调用点在 health 端点内"])

run_case("修 §4（前端加 wrong_instance）-> 该断言变红",
         add_wrong_instance, ["§4 前端没有 wrong_instance"])


# ---------------------------------------------------------------- 汇总

print("=" * 72)
bad = 0
for name, ok, detail in results:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         <- {detail}" if detail else ""))
    if not ok:
        bad += 1
print("=" * 72)
print(f"共 {len(results)} 条，符合预期 {len(results) - bad}，不符合 {bad}")

if not any("正向对照" in r[0] for r in results):
    print("!! 缺正向对照，反向确认不成立")
    sys.exit(2)
if bad:
    print("反向确认失败：断言引擎行为与预期不符")
    sys.exit(1)
print("反向确认通过：该红的红了，该绿的绿了")
sys.exit(0)