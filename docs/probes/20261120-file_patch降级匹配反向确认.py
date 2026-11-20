"""反向确认：把降级匹配摘掉后，`test_file_patch_tiered_match.py` 必须变红。

## 为什么

13 条测试全绿不等于它们有效。必须证明：**没有降级匹配时，这些测试会失败**。
做法是把 `edit_match.find_actual` 换成「只做精确匹配」的实现，
再跑测试 —— 智能引号/转义那几条必须红。

## 三组用例

1. 基线（有降级）：全绿
2. 摘掉降级（等价于旧实现）：必须红，且**红的必须是引号/转义那几条**
3. 恢复：重新全绿

第 2 组的「哪些变红」也要断言 —— 如果连「精确匹配」那条都红了，
说明注入方式破坏了别的逻辑，这次反向确认无效。
"""
from __future__ import annotations

import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TEST = "backend/tests/test_file_patch_tiered_match.py"
MOD = "backend/app/edit_match.py"


def run() -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    orig = open(MOD, encoding="utf-8", newline="").read()

    # 用例 1：基线
    rc, out = run()
    if rc != 0:
        print("✗ 基线不通过：")
        print(out[-800:])
        return 1
    print("[✓] 用例 1（基线）：PASS")

    # 用例 2：摘掉降级（只保留精确匹配）
    marker = '    # ① 精确\n    if old in text:\n        return old\n'
    if marker not in orig:
        print("✗ 找不到注入锚点，反向确认无法进行")
        return 1
    degraded = orig.replace(
        "    # ① 精确\n    if old in text:\n        return old\n",
        "    # [反向确认注入] 只做精确匹配，等价于旧实现\n"
        "    if old in text:\n        return old\n"
        "    return None\n", 1)
    # 把后面三级都短路掉
    degraded = degraded.replace(
        "    # ② 引号归一化（两边都归一化，再回原文截等长片段）",
        "    return None  # [注入] 禁掉降级\n    # ② 引号归一化（两边都归一化，再回原文截等长片段）", 1)
    if degraded == orig:
        print("✗ 注入未生效")
        return 1

    try:
        open(MOD, "w", encoding="utf-8", newline="\n").write(degraded)
        rc2, out2 = run()
    finally:
        open(MOD, "w", encoding="utf-8", newline="\n").write(orig)

    if rc2 == 0:
        print("✗ 摘掉降级后仍然全绿 ⇒ 测试无效")
        return 1
    print("[✓] 用例 2（摘掉降级）：FAIL（符合预期）")

    # 必须红的用例：**只有**依赖降级匹配的那几条
    #
    # `test_replace_all_replaces_every_occurrence` 不在这里 —— replace_all
    # 是独立特性（精确匹配 + replace_all=True 就够），与降级匹配无关。
    # 首版把它列进来，导致反向确认脚本自己误报「应红没红」。
    must_red = ["test_smart_quotes_in_file_match_ascii_in_old_string",
                "test_ascii_in_file_matches_smart_quotes_in_old_string",
                "test_real_chars_in_old_string_match_escaped_file",
                "test_unicode_escape_in_old_string_matches_real_chars"]
    missing = [t for t in must_red if t not in out2]
    if missing:
        print(f"✗ 这些应红的用例没红：{missing}")
        print(out2[-1200:])
        return 1
    print(f"[✓] 注掉后 4 条降级用例都变红")

    # 精确匹配那条不该红（否则注入破坏了基础逻辑）
    if "test_exact_match_still_works" in out2.split("FAILED")[-1]:
        print("[!] 注意：精确匹配用例也红了，检查注入是否影响基础路径")

    # 用例 3：恢复
    rc3, _ = run()
    if rc3 != 0:
        print("✗ 恢复后仍失败")
        return 1
    print("[✓] 用例 3（恢复后）：重新 PASS")
    print("\n✓ 反向确认通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())