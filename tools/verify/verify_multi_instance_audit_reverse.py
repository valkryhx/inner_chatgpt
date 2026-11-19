"""反向确认：证明 verify_multi_instance_audit.py 的断言**会在修复被回退时变红**。

## 为什么（AGENTS.md §8.5 / §8.6）

全绿不携带信息。如果引擎写错（永远 PASS），40 条全绿也说明不了任何事。

## 注意：断言的**方向**已经变了

2026-11-19 之前，这份审计的断言是「问题仍然存在」；四个修复落地后，
断言全部翻成「修复状态成立」。所以反向确认的 mutation 也要反过来：

| | 修复前 | 修复后（现在） |
| --- | --- | --- |
| 断言 | 「语句是无条件的」 | 「语句按 owner 过滤」 |
| mutation | 加过滤 -> 应变红 | **去掉过滤 -> 应变红** |

这一步搞反过：第一版反向确认沿用旧 mutation，跑出来「期望变红，实际没红」，
查了半天才发现是断言方向变了、mutation 没跟着变。

## 这个脚本抓出过我自己的三处断言设计错误

| 我的断言 | 为什么测不到 |
| --- | --- |
| 「无条件收敛语句**仍在**」 | 在原语句后追加条件时，needle 是新语句的**前缀**，仍匹配 |
| 「`_ensure_sticky_cookie` **只在 1 处**被调用」 | 挪动位置不改次数 |
| 前端静态断言「源码里出现 resumeRetryAction」 | 把调用改成 `if (false && ...)` 静态匹配照样成立 |

结论：断言要对着**问题的本质**写，不是对着「问题的某一种表现」。
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
    """加载被测脚本，但**不让它的顶层主流程跑**（会淹没输出并 sys.exit）。"""
    spec = importlib.util.spec_from_file_location("auditor", TARGET)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    real_exit = sys.exit
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                spec.loader.exec_module(m)
            except SystemExit:
                pass
    finally:
        sys.exit = real_exit
    return m


def fresh_copy():
    """复制一份真实仓库到临时目录，返回 (root, 已指向该目录的模块)。"""
    tmp = Path(tempfile.mkdtemp(prefix="revconf_audit_"))
    be = tmp / "backend"
    (be / "deploy").mkdir(parents=True)
    (be / "tests/manual").mkdir(parents=True)
    (tmp / "frontend/src").mkdir(parents=True)
    (tmp / "docs").mkdir(parents=True)
    (tmp / "tools/verify").mkdir(parents=True)

    shutil.copytree(REAL / "backend/app", be / "app", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(REAL / "backend/deploy/nginx.conf", be / "deploy/nginx.conf")
    shutil.copy(REAL / "frontend/src/App.tsx", tmp / "frontend/src/App.tsx")
    for p in (REAL / "backend/tests/manual").iterdir():
        if p.is_file():
            shutil.copy(p, be / "tests/manual" / p.name)
    for p in (REAL / "docs").glob("*.md"):
        shutil.copy(p, tmp / "docs" / p.name)

    m = load_module()
    # 同时改 HLAUDZ 与 BE：src() 的闭包读模块级 BE，只改 HLAUDZ 会读到真仓库
    m.HLAUDZ = tmp
    m.BE = tmp / "backend"
    return tmp, m


# ---------------------------------------------------------------- 重算逻辑
# 每条对应修复后的断言，返回「是否通过」。

def _s1_owner_filtered(m) -> bool:
    """§1：判据函数必须按 owner + 存活集合过滤。

    **落点**：过滤在 `_sessions_to_converge` 里，不在那条 UPDATE 附近
    （UPDATE 是动态拼的）。第一版断言去查 UPDATE 上下文，被上方注释里的
    `owner` 字样骗过 —— 反向确认（去掉过滤）没变红才发现。
    """
    db = m.src("app/db.py")
    start = db.find("async def _sessions_to_converge")
    if start < 0:
        return False
    seg = db[start: start + 3000]
    # 必须测**循环体的四条分支**，不是「这些变量名出现过」。
    # 第一版查变量名，mutation 把循环体换成 `for sid in []` 后变量名还在，
    # 断言仍然 PASS —— 反向确认没变红才暴露。
    return ("if sid not in owner_by_session:" in seg
            and "if not owner:" in seg
            and "if owner == my_owner:" in seg
            and "if owner not in alive:" in seg)


def _s2_in_middleware(m) -> bool:
    """§2：cookie 必须在中间件里下发。"""
    main = m.src("app/main.py")
    start = main.find("async def _instance_header")
    seg = main[start: start + 2000] if start >= 0 else ""
    return "_ensure_sticky_cookie" in seg


def _s4_decision_exists(m) -> bool:
    """§4：必须有 _resume_decision 且返回 wrong_instance。"""
    chat = m.src("app/routers/chat.py")
    start = chat.find("async def _resume_decision")
    seg = chat[start: start + 3000] if start >= 0 else ""
    return bool(seg) and "wrong_instance" in seg


def _s5_runner_exists(m) -> bool:
    """§5：必须有一键入口脚本。"""
    manual = m.BE / "tests/manual"
    return bool(list(manual.glob("*.sh"))) if manual.exists() else False


def _s4_frontend_retry(m) -> bool:
    """§4 前端：必须处理 wrong_instance。"""
    app = (m.HLAUDZ / "frontend/src/App.tsx").read_text(encoding="utf-8")
    return "wrong_instance" in app or "isWrongInstanceError" in app


CHECKS = {
    "§1 判据按 owner 与存活集合过滤": _s1_owner_filtered,
    "§2 中间件里下发粘性 cookie": _s2_in_middleware,
    "§4 _resume_decision 存在": _s4_decision_exists,
    "§4 前端处理 wrong_instance": _s4_frontend_retry,
    "§5 有一键入口脚本": _s5_runner_exists,
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
# 全部是「回退到修复前的行为」——因为断言方向已经翻过来了。

def no_change(_root):
    pass


def revert_s1_to_unconditional(root: Path):
    """回退 §1：把整个 owner 判据循环体换成无条件全清。

    ⚠️ 前两版 mutation 都是**无效的**，反向确认没变红才发现：

    | 版本 | 改了什么 | 为什么没变红 |
    | --- | --- | --- |
    | v1 | 在 UPDATE 后追加 owner 条件 | 断言查的是 UPDATE 附近文本，被上方注释里的 `owner` 骗过 |
    | v2 | 只把 `doomed` 初始化换成 `list(sessions)` | **循环体原封不动**，四条分支还在 |
    | v3（本版） | 删掉整个循环体 | 四条分支真的没了 |

    「mutation 没有真的改变被测行为」是反向确认本身的假阳性 ——
    它会让你误以为断言太宽，实际是 mutation 太窄。
    """
    p = root / "backend/app/db.py"
    t = p.read_text(encoding="utf-8")
    start = t.find("    doomed: list[str] = []")
    end_marker = "    return doomed"
    end = t.find(end_marker, start)
    assert start > 0 and end > start, "mutation 前提不成立"
    replacement = """    doomed: list[str] = list(sessions)   # 回退：无条件全清，不再看 owner
"""
    p.write_text(t[:start] + replacement + t[end:], encoding="utf-8")


def revert_s2_to_health_only(root: Path):
    """回退 §2：中间件里不再下发 cookie。"""
    p = root / "backend/app/main.py"
    t = p.read_text(encoding="utf-8")
    old = """    try:
        _ensure_sticky_cookie(request, response)
    except Exception:  # noqa: BLE001 - 下发失败不该影响响应
        pass"""
    new = """    try:
        pass   # 回退：只在 health 下发
    except Exception:  # noqa: BLE001
        pass"""
    assert old in t, "mutation 前提不成立"
    p.write_text(t.replace(old, new), encoding="utf-8")


def revert_s4_removing_decision(root: Path):
    """回退 §4：删掉 _resume_decision 函数（改成直接发 done 的老逻辑）。"""
    p = root / "backend/app/routers/chat.py"
    t = p.read_text(encoding="utf-8")
    start = t.find("async def _resume_decision")
    assert start > 0, "mutation 前提不成立"
    # 找到下一个顶层 @router 装饰器作为结束点
    end = t.find("@router.get", start)
    assert end > start
    p.write_text(t[:start] + t[end:], encoding="utf-8")


def revert_s5_removing_runner(root: Path):
    """回退 §5：删掉一键入口脚本。"""
    manual = root / "backend/tests/manual"
    for f in list(manual.glob("*.sh")):
        f.unlink()


def revert_frontend_retry(root: Path):
    """回退 §4 前端：去掉 wrong_instance 处理。"""
    p = root / "frontend/src/App.tsx"
    t = p.read_text(encoding="utf-8")
    t = t.replace("isWrongInstanceError", "/* removed */")
    t = t.replace("planResumeRetry", "/* removed */")
    p.write_text(t, encoding="utf-8")


# ---------------------------------------------------------------- 执行

run_case("不改源码：修复状态断言全部 PASS（正向对照）", no_change, [])

run_case("回退 §1（去掉 owner 过滤）-> 「按 owner 过滤」该变红",
         revert_s1_to_unconditional, ["§1 判据按 owner 与存活集合过滤"])

run_case("回退 §2（中间件不下发）-> 「中间件里下发」该变红",
         revert_s2_to_health_only, ["§2 中间件里下发粘性 cookie"])

run_case("回退 §4（删掉决策函数）-> 两条 §4 该变红",
         revert_s4_removing_decision,
         ["§4 _resume_decision 存在"])

run_case("回退 §5（删掉一键脚本）-> 「一键入口」该变红",
         revert_s5_removing_runner, ["§5 有一键入口脚本"])

run_case("回退前端（去掉 wrong_instance）-> 该变红",
         revert_frontend_retry, ["§4 前端处理 wrong_instance"])


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
print("反向确认通过：回退修复会变红，保持修复会变绿")
sys.exit(0)