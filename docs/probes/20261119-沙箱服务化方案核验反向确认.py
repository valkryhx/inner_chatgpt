"""反向确认：《沙箱服务化方案（bwrap 版）》核验必须能变红。

AGENTS.md §8.6：断言全绿不携带信息，必须证明它**会**失败。
并且必须同时有对照组（证明不是「永远红」，全红同样不携带信息）。

## 顺序要求

本文件的 `main()` 必须保持薄：所有 `def` 都在模块级。
（本项目实际踩过：`def` 插在 main() 中间把 main() 截断，
`sys.exit(main())` 拿到 None ⇒ 退出码 0，一个判假绿的脚本自己被假绿骗了。）
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TARGET = REPO / "docs" / "probes" / "20261119-沙箱服务化方案核验.py"
DOC = REPO / "docs" / "20261119-沙箱服务化方案-bwrap版.md"

results: list[tuple[str, bool, str]] = []


def run_against(doc_text: str | None) -> tuple[int, str]:
    """把文档换成 doc_text（None = 原文档），在真实仓库上跑核验。"""
    with tempfile.TemporaryDirectory() as td:
        doc_path = Path(td) / "doc.md"
        doc_path.write_text(
            doc_text if doc_text is not None else DOC.read_text(encoding="utf-8"),
            encoding="utf-8", newline="\n")
        env = dict(os.environ)
        env["HLZ_PLAN_REPO"] = str(REPO)
        env["HLZ_PLAN_DOC"] = str(doc_path)
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
        print("        输出尾部:", out[-400:].replace("\n", " | "))


def group_doc() -> None:
    print()
    print("=" * 70)
    print("第一组：篡改文档，核验必须变红")
    print("=" * 70)

    text = DOC.read_text(encoding="utf-8")

    case("对照：原文档 -> 绿", doc_text=None, expect_fail=False)

    case("文档截断 -> 红",
         doc_text="# 空\n",
         expect_fail=True, must_mention="文档存在且非空")

    case("删掉「未经运行时验证」-> 红",
         doc_text=text.replace("未经运行时验证", "已验证"),
         expect_fail=True, must_mention="未经运行时验证")

    case("把四种池形态写回三种 -> 红",
         doc_text=text.replace("四种池形态", "三种池形态"),
         expect_fail=True, must_mention="四种池形态")

    case("删掉纯净 fork 结论 -> 红",
         doc_text=text.replace("纯净父进程", "复用父进程"),
         expect_fail=True, must_mention="纯净父进程")

    case("删掉「不要 MinIO」结论 -> 红",
         doc_text=text.replace("不要 MinIO", "用 MinIO"),
         expect_fail=True, must_mention="不要 MinIO")

    case("删掉订正章节 -> 红",
         doc_text=text.replace("订正", "说明"),
         expect_fail=True, must_mention="订正")

    case("删掉回滚硬要求 -> 红",
         doc_text=text.replace("回滚能力是硬要求", "回滚可行"),
         expect_fail=True, must_mention="回滚能力是硬要求")

    case("实测数字被改 -> 红",
         doc_text=text.replace("5528 ms", "900 ms"),
         expect_fail=True, must_mention="5528 ms")

    case("删掉 bwrap 的 --tmpfs /etc -> 红",
         doc_text=text.replace("--tmpfs /etc", "--bind /etc"),
         expect_fail=True, must_mention="--tmpfs /etc")

    # --- 新增章节的注入（这些是选 A 留下的痕迹，缺一条就说明决策没落地）---
    case("删掉 §2.5 对照章节 -> 红",
         doc_text=text.replace("## 2.5 与", "## 2.5b 与"),
         expect_fail=True, must_mention="§2.5")

    case("不承认 bwrap 没有内核边界 -> 红",
         doc_text=text.replace("没有独立内核攻击面", "有独立内核攻击面"),
         expect_fail=True, must_mention="承认 bwrap 没有内核攻击面边界")

    case("删掉 Executor 接口章节 -> 红",
         doc_text=text.replace("## 8.2 Executor 接口", "## 8.2x Executor"),
         expect_fail=True, must_mention="Executor 接口")

    case("删掉 GVisorExecutor 实现 -> 红",
         doc_text=text.replace("GVisorExecutor", "FutureExecutor"),
         expect_fail=True, must_mention="GVisorExecutor")

    # ⚠️ must_mention 必须是**核验脚本打印的检查项名**，不是文档原文。
    # 写文档原文会得到假 FAIL（rc 已经对了，只是文本没在输出里）。
    case("删掉「路线选 A」决策 -> 红",
         doc_text=text.replace("先 bwrap，后补 gVisor", "待定"),
         expect_fail=True, must_mention="写明路线选 A")

    case("删掉内部用户的定位 -> 红",
         doc_text=text.replace("内部用户", "外部用户").replace("内部可信", "外部不可信"),
         expect_fail=True, must_mention="内部")

    # --- §9 实施阶段的注入（缺一段就说明计划没写完）---
    case("删掉阶段 3 的「不做什么」-> 红",
         doc_text=text.replace("#### 3.4 不做什么", "#### 3.4 补充"),
         expect_fail=True, must_mention="阶段 3 有「不做什么」")

    # ⚠️ must_mention 一律写**核验脚本打印的检查项名**，不写文档原文。
    # 写文档原文会得到假 FAIL：rc 已经变红了（断言确实有效），
    # 只是那句原文没出现在核验输出里。这类假 FAIL 会让人去改本来没错的东西。
    case("删掉阶段 2 的反向测试 -> 红",
         doc_text=text.replace("test_service_backend_is_actually_used", "some_test"),
         expect_fail=True, must_mention="service 后端真的被用了")

    case("删除 fork 三条铁律 -> 红",
         doc_text=text.replace("三条铁律", "几点说明"),
         expect_fail=True, must_mention="三条铁律")

    case("删掉跨租户内存泄漏测试 -> 红",
         doc_text=text.replace("test_no_cross_tenant_memory_leak", "test_mem"),
         expect_fail=True, must_mention="跨租户内存泄漏测试")

    case("off 模式验收被弱化 -> 红",
         doc_text=text.replace("网络层 0 次调用", "行为正确"),
         expect_fail=True, must_mention="网络层 0 次调用")

    case("删掉「阶段 1 不做池」-> 红",
         doc_text=text.replace("不做池（阶段 3）", "做池"),
         expect_fail=True, must_mention="阶段 1 明确不做池")

    # --- §4 池形态的注入（这是「一个还是一个池」这个问题的答案）---
    case("不明确说「是池」-> 红",
         doc_text=text.replace("是池，而且必须是池", "看情况"),
         expect_fail=True, must_mention="明确回答是池")

    case("删掉「槽不能轮换用户」的实测 -> 红",
         doc_text=text.replace("unshare(CLONE_NEWNS)=EPERM", "无法重挂"),
         expect_fail=True, must_mention="不能轮换服务多个用户")

    case("删掉内存预算章节 -> 红",
         doc_text=text.replace("### 4.5 内存预算", "### 4.5 内存"),
         expect_fail=True, must_mention="内存预算")

    case("把单槽内存写成 RSS（错误口径）-> 红",
         doc_text=text.replace("49 MB", "89 MB"),
         expect_fail=True, must_mention="单槽真实占用")

    case("删掉「RSS 会重复计算」的说明 -> 红",
         doc_text=text.replace("重复计算共享库页", "计算"),
         expect_fail=True, must_mention="重复计算共享页")

    # --- §4.7 动态扩缩容的注入 ---
    case("删掉 §4.7 扩缩容章节 -> 红",
         doc_text=text.replace("### 4.7 动态扩缩容", "### 4.7 扩容"),
         expect_fail=True, must_mention="§4.7 扩缩容章节")

    case("把 MAX_SLOTS 说成固定池大小 -> 红",
         doc_text=text.replace("保护阀", "池大小"),
         expect_fail=True, must_mention="MAX_SLOTS 是保护阀")

    case("删掉 queue_wait_ms -> 红",
         doc_text=text.replace("queue_wait_ms", "wait"),
         expect_fail=True, must_mention="queue_wait_ms")

    case("删掉「不淘汰忙碌槽」-> 红",
         doc_text=text.replace("- ❌ **不淘汰忙碌中的槽**（会掐断正在跑的代码）", ""),
         expect_fail=True, must_mention="不淘汰忙碌槽")

    case("删掉 LRU 淘汰测试 -> 红",
         doc_text=text.replace("test_overflow_evict_removes_lru_idle_slot", "test_evict"),
         expect_fail=True, must_mention="test_overflow_evict_removes_lru_idle_slot")

    case("删掉「不能预建通用槽」-> 红",
         doc_text=text.replace("不能：预先建「通用槽」", "不能预建"),
         expect_fail=True, must_mention="不能预建通用槽")

    # --- OVERFLOW 三分支（三个取值的含义必须都在，缺一个就说明没写清）---
    case("删掉 OVERFLOW 的 evict 取值 -> 红",
         doc_text=text.replace("`evict`", "`b`"),
         expect_fail=True, must_mention="OVERFLOW 取值")

    case("删掉 OVERFLOW 默认值说明 -> 红",
         doc_text=text.replace("**`queue`**（默认）", "`queue`"),
         expect_fail=True, must_mention="OVERFLOW 默认是 queue")

    case("删掉 evict 全忙退化说明 -> 红",
         doc_text=text.replace("退化成 `queue`", "改成别的方式"),
         expect_fail=True, must_mention="退化成 queue")

    case("删掉「忙碌槽永不淘汰」共同点 -> 红",
         doc_text=text.replace("忙碌中的槽永不淘汰", "按策略处理"),
         expect_fail=True, must_mention="忙碌槽永不淘汰")

    case("删掉非法值兜底 -> 红",
         doc_text=text.replace("非法取值一律退回 `queue`", "非法值报错"),
         expect_fail=True, must_mention="非法 OVERFLOW 取值退回 queue")

    case("删掉 §6.2.1 组合取值表 -> 红",
         doc_text=text.replace("**③ `HLZ_SANDBOX_OVERFLOW`**", "**③ 溢出**"),
         expect_fail=True, must_mention="§6.2.1 有 OVERFLOW 取值表")

    case("删掉 reject 取值的代价说明 -> 红",
         doc_text=text.replace("直接失败", "失败"),
         expect_fail=True, must_mention="reject 写了代价")


def group_probe() -> None:
    """篡改**探针本体**，证明核验真的会检查文件而不是只看文档。"""
    print()
    print("=" * 70)
    print("第二组：篡改探针脚本 / 源码，核验必须变红")
    print("=" * 70)

    probe = REPO / "docs" / "probes" / "20261119-沙箱服务化方案核验.py"
    original = probe.read_text(encoding="utf-8")
    try:
        # 把 VALID_MODES 的断言改成一个永远不成立的行号
        probe.write_text(
            original.replace('l = line_of(cfg, 30)', 'l = line_of(cfg, 99999)'),
            encoding="utf-8", newline="\n")
        case("探针改成查不存在的行 99999 -> 红", doc_text=None,
             expect_fail=True, must_mention="config.py:30")
    finally:
        probe.write_text(original, encoding="utf-8", newline="\n")

    try:
        # 把 must_not 的入参写反（本项目实际踩过的静默错误）
        probe.write_text(
            original.replace('must_not("backend 里没有 minio / boto3 依赖", hits == 0',
                             'must_not("backend 里没有 minio / boto3 依赖", hits >= 0'),
            encoding="utf-8", newline="\n")
        case("探针把 must_not 写反 -> 仍绿（证明这类错误是静默的）",
             doc_text="x" if False else None,
             expect_fail=False)
    finally:
        probe.write_text(original, encoding="utf-8", newline="\n")

    # 对照组：探针不动，必须绿
    case("对照：探针不动 -> 绿", doc_text=None, expect_fail=False)


def main() -> int:
    print("=" * 70)
    print("反向确认：方案核验脚本必须能变红")
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
    print("（含对照组：证明全绿时确实会绿，不是永远红）")
    return 0


if __name__ == "__main__":
    sys.exit(main())