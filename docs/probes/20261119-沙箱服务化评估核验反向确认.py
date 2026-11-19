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



def probe_case(name: str, mutate, expect_fail: bool, must_mention: str = "") -> None:
    """对**探针脚本本体**做篡改，跑核验，期望变红。

    这一组专门证明核验脚本真的会去检查探针（而不是只看文档写了什么）。
    篡改后无论成败都还原，避免留下脏文件。
    """
    probe = REPO / "docs" / "probes" / "20261119-bwrap可行性实测.sh"
    original = probe.read_text(encoding="utf-8")
    try:
        probe.write_text(mutate(original), encoding="utf-8", newline="\n")
        code, out = run_against(None)
    finally:
        probe.write_text(original, encoding="utf-8", newline="\n")
    got_fail = code != 0
    ok = (got_fail == expect_fail) and (not must_mention or must_mention in out)
    results.append((name, ok, f"rc={code}"))
    mark = "PASS" if ok else "**FAIL**"
    want = "红" if expect_fail else "绿"
    print(f"  [{mark}] {name}  (期望{want}, 实际 rc={code})")
    if not ok:
        print("        输出尾部:", out[-500:].replace("\n", " | "))


def group_doc() -> None:
    """第一组：篡改**文档**，核验必须变红。"""
    print()
    print("=" * 70)
    print("第一组：篡改文档，核验必须变红")
    print("=" * 70)

    text = DOC.read_text(encoding="utf-8")

    # 对照组：原文档必须通过（证明不是「永远红」）
    case("对照：原文档 -> 绿", doc_text=None, expect_fail=False)

    # 注入 1：写回已被修正的旧行号
    case("注入旧坐标 runtime.py:124 -> 红",
         doc_text=text + "\n参考 app/sandbox/runtime.py:124 附近\n",
         expect_fail=True, must_mention="runtime.py:124")

    # 注入 2：删掉免责声明
    case("删除免责声明 -> 红",
         doc_text=text.replace("未经运行时验证", "已验证"),
         expect_fail=True, must_mention="未经运行时验证")

    # 注入 3：抹掉「实测 / 推断」的区分
    case("删除实测/推断区分 -> 红",
         doc_text=text.replace("实测", "").replace("推断", ""),
         expect_fail=True, must_mention="实测")

    # 注入 4：文档截断
    case("文档被截断 -> 红",
         doc_text="# 空文档\n",
         expect_fail=True, must_mention="文档存在且非空")

    # 注入 5：删掉整个 §7 运行时选型章节
    case("删掉 §7 运行时选型 -> 红",
         doc_text=text.split("## 7. 运行时选型")[0],
         expect_fail=True, must_mention="§7 章节存在")

    # 注入 6：§7 漏记「假降权」坑
    case("§7 漏记假降权坑 -> 红",
         doc_text=text.replace("假降权", "降权"),
         expect_fail=True, must_mention="假降权")

    # 注入 7：§7 漏记 prlimit 不是组总量
    case("§7 漏记只管单进程 -> 红",
         doc_text=text.replace("只管单进程", "资源限制"),
         expect_fail=True, must_mention="只管单进程")

    # 注入 8：§7 不再标成实测
    #
    # 这条是**实际抓到过的假绿**：最初只靠全文级的「文档区分了实测与推断」
    # 判定，而「实测」二字在文档别处到处都是 —— 把 §7 的实测标注删掉，
    # 全文级断言照样绿。所以要在核验脚本里给 §7 单钉一条。
    case("§7 丢掉实测标注 -> 红",
         doc_text=text.replace("第 7 章（运行时选型）**是实测**", "第 7 章"),
         expect_fail=True, must_mention="§7 显式标注自己是实测")

    # 注入 9：§7 丢掉「未被生产验证」的边界说明
    case("§7 丢掉生产验证边界 -> 红",
         doc_text=text.replace("所有验证都在「容器内跑 bwrap」", "已验证于生产"),
         expect_fail=True, must_mention="§7 显式标注未被生产验证")


def group_probe() -> None:
    """第二组：篡改**探针脚本**，核验必须变红。"""
    print()
    print("=" * 70)
    print("第二组：篡改探针脚本，核验必须变红")
    print("=" * 70)

    probe_case("探针删掉 ok 10b 断言 -> 红",
               lambda s: s.replace("ok 10b", "ok 10b-REMOVED"),
               expect_fail=True, must_mention="ok 10b")

    probe_case("探针删掉 ok 12 断言 -> 红",
               lambda s: s.replace("ok 12", "ok 12-REMOVED"),
               expect_fail=True, must_mention="ok 12")

    # 退回「输出文本比对」的坏写法 —— 本次实际踩到的假红
    probe_case("探针退回文本比对判 mount -> 红",
               lambda s: s.replace(
                   "mount -t tmpfs none /mnt 2>/dev/null; echo $?",
                   "mount -t tmpfs none /mnt 2>&1 >/dev/null && echo ok || echo denied"),
               expect_fail=True, must_mention="输出文本比对")

    # 退回用 $PROBE 作 -v 挂载源 —— 本次实际踩到的假信号（探针一行没跑）
    probe_case("探针退回 PROBE 作挂载源 -> 红",
               lambda s: s.replace('-v "$PROBE_WIN/root.sh:/probe.sh:ro"',
                                   '-v "$PROBE/root.sh:/probe.sh:ro"'),
               expect_fail=True, must_mention="PROBE")

    # 对照组：探针不动，必须绿
    probe_case("对照：探针不动 -> 绿", lambda s: s, expect_fail=False)


def main() -> int:
    """两组合计。

    ⚠️ 结构坑（本次实际踩到，写在这里防复发）：一度把 `probe_case` 的 def
    直接插在 main() 中间，main() 被截成两半 —— 后半段变成了死代码，
    `sys.exit(main())` 拿到的是截断版 main()，它 return None ⇒ **退出码 0**。
    表现是只跑 9 个用例、第二组一个没跑、汇总不打印，而退出码是 0。
    一个专门用来判「绿是不是假绿」的脚本，自己被结构错骗了。
    现在所有 def 都在模块级，main() 只做串接。
    """
    print("=" * 70)
    print("反向确认：核验脚本对错误注入必须变红")
    print("=" * 70)

    group_doc()
    group_probe()

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
    print("（含 2 个对照组：证明全绿时确实会绿，不是永远红）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
