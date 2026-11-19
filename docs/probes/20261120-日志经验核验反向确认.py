"""反向确认：《日志经验》核验必须能变红。

AGENTS.md §8.6：断言全绿不携带信息，必须证明它会失败，
且必须有一个「应该 PASS」的对照组（否则全红同样不携带信息）。

## 本文件的顺序要求

`main()` 保持薄，所有 `def` 在模块级。
（本项目实际踩过：`def` 插在 main() 中间把 main() 截断，
`sys.exit(main())` 拿到 None ⇒ 退出码 0，判假绿的脚本自己被假绿骗了。）
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TARGET = REPO / "docs" / "probes" / "20261120-日志经验核验.py"
DOC = REPO / "docs" / "20261120-Python日志静默丢失排查经验.md"

results: list[tuple[str, bool]] = []


def run_against(doc_text: str | None) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as td:
        doc_path = Path(td) / "doc.md"
        doc_path.write_text(
            doc_text if doc_text is not None else DOC.read_text(encoding="utf-8"),
            encoding="utf-8", newline="\n")
        env = dict(os.environ)
        env["HLZ_LOGEXP_REPO"] = str(REPO)
        env["HLZ_LOGEXP_DOC"] = str(doc_path)
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run(
            [sys.executable, str(TARGET)],
            capture_output=True, text=True, timeout=120, env=env,
            encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")


def case(name: str, *, doc_text, expect_fail: bool, must_mention: str = "") -> None:
    code, out = run_against(doc_text)
    got_fail = code != 0
    ok = (got_fail == expect_fail) and (not must_mention or must_mention in out)
    results.append((name, ok))
    mark = "PASS" if ok else "**FAIL**"
    want = "红" if expect_fail else "绿"
    print(f"  [{mark}] {name}  (期望{want}, 实际 rc={code})")
    if not ok:
        print("        输出尾部:", out[-350:].replace("\n", " | "))


def group_doc() -> None:
    print()
    print("=" * 70)
    print("第一组：篡改文档，核验必须变红")
    print("=" * 70)

    text = DOC.read_text(encoding="utf-8")

    case("对照：原文档 -> 绿", doc_text=None, expect_fail=False)

    case("文档截断 -> 红", doc_text="# 空\n", expect_fail=True,
         must_mention="文档存在且非空")

    case("把结论写回「uvicorn 的 bug」-> 红",
         doc_text=text.replace("不是 uvicorn 的 bug", "是 uvicorn 的 bug"),
         expect_fail=True, must_mention="不是 uvicorn 的 bug")

    # ⚠️ must_mention 一律写**核验脚本打印的检查项名**，不写文档原文。
    # 写文档原文会得到假 FAIL：rc 已经变红（断言确实有效），
    # 只是那句原文没出现在输出里。本项目已因此误判过三次。
    case("删掉「配包根 logger」-> 红",
         doc_text=text.replace("包根", "logger"),
         expect_fail=True, must_mention="文档要求配包根 logger")

    case("删掉幂等检查 -> 红",
         doc_text=text.replace("if root.handlers", "if False"),
         expect_fail=True, must_mention="if root.handlers")

    case("删掉 basicConfig 反面说明 -> 红",
         doc_text=text.replace("basicConfig", "configure"),
         expect_fail=True, must_mention="basicConfig")

    case("删掉排查清单 -> 红",
         doc_text=text.replace("排查清单", "步骤"),
         expect_fail=True, must_mention="排查清单")

    case("删掉订正声明 -> 红",
         doc_text=text.replace("订正记在这里", "记录在此").replace("订正是", "修正"),
         expect_fail=True, must_mention="订正")


def group_impl() -> None:
    """篡改核验脚本本身，证明它真的会检查文件内容。"""
    print()
    print("=" * 70)
    print("第二组：篡改核验脚本，必须变红")
    print("=" * 70)

    original = TARGET.read_text(encoding="utf-8")
    try:
        # 让「文档含关键结论」检查一个不存在的串
        TARGET.write_text(
            original.replace('"不是 uvicorn 的 bug"', '"绝不存在的字符串XYZ"'),
            encoding="utf-8", newline="\n")
        case("核验查不存在的串 -> 红", doc_text=None, expect_fail=True)
    finally:
        TARGET.write_text(original, encoding="utf-8", newline="\n")

    # 对照组：脚本不动，必须绿
    case("对照：核验脚本不动 -> 绿", doc_text=None, expect_fail=False)


def main() -> int:
    print("=" * 70)
    print("反向确认：日志经验核验必须能变红")
    print("=" * 70)

    group_doc()
    group_impl()

    print()
    print("=" * 70)
    bad = [n for n, ok in results if not ok]
    if bad:
        print(f"结果：{len(bad)} 个用例不符合预期")
        for n in bad:
            print(f"  - {n}")
        return 1
    print(f"结果：{len(results)}/{len(results)} 用例符合预期")
    print("（含 2 个对照组：证明全绿时确实会绿，不是永远红）")
    return 0


if __name__ == "__main__":
    sys.exit(main())