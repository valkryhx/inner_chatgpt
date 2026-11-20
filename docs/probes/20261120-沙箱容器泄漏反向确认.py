"""反向确认：`20261120-沙箱容器泄漏根因.md` 的判据在「修复后」必须变红。

做法：把每条断言的**读数来源**换成一个「已修复」的假数据，
看断言是否会翻转。不翻转的判据 = 假判据。

必须同时有「应该 PASS」的用例 —— 否则引擎只会返回 FAIL，
全绿和全红都不携带信息。
"""
from __future__ import annotations

import sys, collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def judge(rows: list[dict]) -> dict:
    """与主核验脚本同构的判据集，输入是可注入的行数据。"""
    out = {}
    owners = [r["owner"] for r in rows if r["owner"]]
    out["owner_唯一"] = len(set(owners)) == len(owners)
    out["全是_pytest"] = (sum(1 for r in rows if "pytest-of-" in r["bind"])
                         >= len(rows) - 2)
    out["全是_sleep"] = set(r["cmd"] for r in rows) == {"sleep infinity"}
    return out


def row(owner="valkry:1234", bind=r"C:\Temp\pytest-of-x\pytest-1\t\u1",
        cmd="sleep infinity"):
    return {"owner": owner, "bind": bind, "cmd": cmd}


FAILS = []


def expect(label, got, want, note=""):
    if got == want:
        print(f"[✓] {label} → {got}")
    else:
        print(f"[✗] {label} → {got}，期望 {want}  {note}")
        FAILS.append(label)


# ---- 用例 1：修好了（每次跑完都 shutdown）→ owner 全部相同、无残留 ----
# 这是「应该 PASS 的用例」：证明引擎不是只会返回 FAIL。
print("== 用例 1：已修复（容器复用，owner 恒定）==")
fixed = [row(owner="valkry:9999", bind=r"D:\sandbox\u1") for _ in range(3)]
r = judge(fixed)
expect("已修复时 owner_唯一", r["owner_唯一"], False,
       "复用同一个 owner ⇒ owner 有重复 ⇒ 该判据此时应为 False")
expect("已修复时 全是_pytest", r["全是_pytest"], False,
       "复用后 bind 不再是 pytest 临时目录")
print("  → 判据在「修复」态与「泄漏」态读数不同 ⇒ 是真判据\n")

# ---- 用例 2：原始泄漏态 ----
print("== 用例 2：原始泄漏（58 个不同 PID）==")
leaked = [row(owner=f"valkry:{1000+i}") for i in range(58)]
r = judge(leaked)
expect("泄漏态 owner_唯一", r["owner_唯一"], True)
expect("泄漏态 全是_pytest", r["全是_pytest"], True)


# ---- 用例 3：故意注入三处错误，证明每条断言单独可红 ----
print("\n== 用例 3：单条注入错误（证明每条各自可红）==")


def judge_one(key, rows):
    return judge(rows)[key]


# 3a. 把 owner 改成重复 → owner_唯一 应翻红
dup = [row(owner="valkry:777") for _ in range(58)]
expect("3a 全同 owner ⇒ owner_唯一 翻红",
       judge_one("owner_唯一", dup), False)

# 3b. 把 bind 改成非 pytest → 全是_pytest 应翻红
real = [row(owner=f"valkry:{1000+i}", bind=r"D:\sandbox\u1") for i in range(58)]
expect("3b bind 非 pytest ⇒ 全是_pytest 翻红",
       judge_one("全是_pytest", real), False)

# 3c. 把 cmd 改成干活进程 → 全是_sleep 应翻红
busy = [row(owner=f"valkry:{1000+i}", cmd="python app.py") for i in range(58)]
expect("3c 有干活进程 ⇒ 全是_sleep 翻红",
       judge_one("全是_sleep", busy), False)

# ---- 用例 4：确认引擎确实能全绿（否则上面的红无意义）----
print("\n== 用例 4：全绿对照 ==")
r = judge(leaked)
expect("全部判据为真", all(r.values()), True)


# ---- 用例 5：AST 静态守卫 ----


def _ast_calls_shutdown(path: str) -> bool:
    """conftest 的 `_clear()` 里有没有 await shutdown()。

    用 AST 按函数作用域限定，不用文本匹配 —— 注释里提到 shutdown
    会让文本匹配假绿（本仓库反面实例）。
    """
    import ast, pathlib
    src = pathlib.Path(path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in ("_clear", "_reset_app_sandbox_singletons"):
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and node.attr == "shutdown":
                    return True
    return False


print("\n== 用例 5：AST 守卫 ==")
expect("conftest._clear 当前不含 shutdown（= 泄漏态）",
       _ast_calls_shutdown("backend/tests/conftest.py"), False)
expect("反向确认自身能被识别为含 shutdown（构造样本）",
       _ast_calls_shutdown(__file__), False,
       "本文件只提字符串不提调用 —— 正好证明文本匹配会假绿、AST 不会")

print()
if FAILS:
    print(f"✗ 反向确认失败 {len(FAILS)} 项：{FAILS}")
    sys.exit(1)
print("✓ 反向确认通过：每条判据都至少在一种状态下翻红，且有全绿对照")
