"""反向确认：《沙箱服务化评估》核验脚本必须能变红。

AGENTS.md §8.6：断言全绿不携带信息，必须证明它**会**失败。

## 用例设计

必须同时包含：
- 应该 FAIL 的注入（证明能变红）
- 应该 PASS 的对照（证明不是「永远红」，全红同样不携带信息）

## 实现方式

核验脚本支持 `HLZ_ASSESS_REPO` / `HLZ_ASSESS_DOC` 环境变量覆盖路径。
这里把文档换成注入版本、路径指向真实仓库，再跑原脚本 —— 这样校验的
仍是**真实的源码坐标**，只换被测文档。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TARGET = REPO / "docs" / "probes" / "20261119-沙箱服务化评估核验.py"
DOC = REPO / "docs" / "20261119-沙箱服务化评估.md"

results: list[tuple[str, bool, str]] = []


def run_against(doc_text: str | None = None) -> tuple[int, str]:
    """把文档替换成 doc_text（None = 原文档），在真实仓库上跑核验。"""
    with tempfile.TemporaryDirectory() as td:
        doc_path = doc_text if isinstance(doc_text, Path) else Path(td) / "doc.md"
        if not isinstance(doc_text, Path):
            doc_path.write_text(
                doc_text if doc_text is not None
                else DOC.read_text(encoding="utf-8"),
                encoding="utf-8", newline="\n")
        env = dict(os.environ)
        env["HLZ_ASSESS_REPO"] = str(REPO)
        env["HLZ_ASSESS_DOC"] = str(doc_path)
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run(
            [sys.executable, str(TARGET)],
            capture_output=True, text=True, timeout=180, env=env,
            encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")


def case(name: str, *, doc_text, expect_fail: bool, must_mention: str = "") -> None:
    code, out = run_against(doc_text)
    got_fail = code != 0
    ok = (got_fail == expect_fail) and (not must_mention or must_mention in out)
    results.append((name, ok, f"rc={code}"))
    mark = "PASS" if ok else "**FAIL**"
    want = "红" if expect_fail else "绿"
    print(f"  [{mark}] {name}  (期望{want}, 实际 rc={code})")
    if not ok:
        print("        输出尾部:", out[-500:].replace("\n", " | "))


def main() -> int:
    print("=" * 70)
    print("反向确认：核验脚本对错误注入必须变红")
    print("=" * 70)

    text = DOC.read_text(encoding="utf-8")

    # --- 对照组：原文档必须通过（证明不是「永远红」）---
    case("对照：原文档 -> 绿", doc_text=None, expect_fail=False)

    # --- 注入 1：文档里写回旧行号 runtime.py:124 ---
    case("注入旧坐标 runtime.py:124 -> 红",
         doc_text=text + "\n参考 app/sandbox/runtime.py:124 附近\n",
         expect_fail=True, must_mention="runtime.py:124")

    # --- 注入 2：删掉「未经运行时验证」的免责声明 ---
    case("删除免责声明 -> 红",
         doc_text=text.replace("未经运行时验证", "已验证"),
         expect_fail=True, must_mention="未经运行时验证")

    # --- 注入 3：删掉「实测」与「推断」的区分 ---
    case("删除实测/推断区分 -> 红",
         doc_text=text.replace("实测", "").replace("推断", ""),
         expect_fail=True, must_mention="实测")

    # --- 注入 4：文档内容截断 ---
    case("文档被截断 -> 红",
         doc_text="# 空文档\n",
         expect_fail=True, must_mention="文档存在且非空")

    print()
    print("=" * 70)
    bad = [n for n, ok, _ in results if not ok]
    if bad:
        print(f"结果：{len(bad)} 个用例不符合预期")
        for n in bad:
            print(f"  - {n}")
        return 1
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"结果：{passed}/{len(results)} 用例符合预期")
    print("（含 1 个对照组：证明全绿时确实会绿，不是永远红）")
    return 0


if __name__ == "__main__":
    sys.exit(main())