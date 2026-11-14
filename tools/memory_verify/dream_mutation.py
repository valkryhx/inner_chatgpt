"""反向确认（变异测试）：把 dream 的关键机制逐个改坏，看对应测试是否变红。

跑法::
    cd backend && python ../tools/memory_verify/dream_mutation.py

每条变异都必须让**指定的**测试失败；如果某条变异之后测试仍然全绿，
说明那条测试没测到东西 —— 这比测试失败严重得多。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
SRC = BASE / "backend" / "app" / "memory_dream.py"
BACKEND = BASE / "backend"

# (说明, 原文, 替换为, 应该变红的测试)
MUTATIONS = [
    (
        "删掉「先写后删」闸（prompt 说过就够了）",
        "        if budget.writes == 0:",
        "        if False:",
        "test_delete_refused_before_any_successful_write",
    ),
    (
        "删掉「先读后删」闸（没读过也给删）",
        "        if seen is None:",
        "        if False:",
        "test_delete_refused_if_not_read_first",
    ),
    (
        "删掉「读后未变」闸（删除不检查版本）",
        "        if seen != current.version:",
        "        if False:",
        "test_delete_refused_if_changed_after_read",
    ),
    (
        "失败也推进 last_consolidated（CC 的做法）",
        "            await _set_state(user_id, last_status=STATUS_FAILED,\n"
        "                             last_error=out.error,",
        "            await _set_state(user_id, last_status=STATUS_FAILED,\n"
        "                             last_error=out.error, last_consolidated=_now_ms(),",
        "test_m21_failure_does_not_advance_last_consolidated",
    ),
    (
        "启动收敛不清锁（只靠 30 分钟过期）",
        "\"UPDATE memory_dream_state SET lock_pid=0, lock_at=0, \"",
        "\"UPDATE memory_dream_state SET last_error=last_error, \"",
        "test_m22_recover_clears_fresh_lock",
    ),
    (
        "巡检不补 index_pending",
        "        pending = await list_users_with_pending_index()",
        "        pending = []",
        "test_m23_killed_before_phase4_only_redoes_index",
    ),
    (
        "锁能被任何持有者重复抢（去掉原子条件）",
        "\"WHERE user_id=? AND (lock_pid=0 OR lock_at=0 OR lock_at<?)\"",
        "\"WHERE user_id=? AND (lock_pid<>0 OR lock_at=0 OR lock_at<?)\"",
        "test_m18_fresh_lock_is_not_stolen",
    ),
    (
        "会话数闸看历史总数（不看 since）",
        "\"SELECT id FROM sessions WHERE user_id=? AND updated_at>=? ORDER BY updated_at DESC\"",
        "\"SELECT id FROM sessions WHERE user_id=? AND ?>=0 ORDER BY updated_at DESC\"",
        "test_m17_sessions_gate_counts_only_since_last_consolidation",
    ),
    (
        "删除配额不生效",
        "        if len(budget.deleted) >= budget.limit:",
        "        if False:",
        "test_delete_budget_is_capped",
    ),
    (
        "失败时不立 index_pending",
        "                             index_pending=1 if (out.written or out.deleted) else 0)",
        "                             index_pending=0)",
        "test_m23_failed_run_with_writes_sets_index_pending",
    ),
    (
        "首次整理也受 minHours 约束（新用户永远等不到首次）",
        "        if now - since < min_hours() * 3600 * 1000:",
        "        if not since or now - since < min_hours() * 3600 * 1000:",
        "test_m17_first_run_does_not_wait_min_hours",
    ),
]


def run_test(name: str) -> bool:
    """返回 True = 通过。"""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", f"tests/test_memory_dream.py::{name}", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=BACKEND, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.returncode == 0


def main() -> int:
    original = SRC.read_text(encoding="utf-8")
    failures: list[str] = []
    try:
        # 先确认基线全绿
        if not run_test("test_m19_index_respects_caps"):
            print("[FAIL] 基线就是红的，先修测试再谈变异")
            return 1
        for desc, old, new, test_name in MUTATIONS:
            if old not in original:
                print(f"[SKIP] 原文没找到（变异失效）：{desc}")
                failures.append(f"{desc}（原文不匹配）")
                continue
            SRC.write_text(original.replace(old, new, 1), encoding="utf-8", newline="\n")
            passed = run_test(test_name)
            SRC.write_text(original, encoding="utf-8", newline="\n")
            mark = "FAIL" if passed else "OK  "
            print(f"[{mark}] {desc} -> {test_name} "
                  f"{'仍然通过（测试没测到东西）' if passed else '变红'}")
            if passed:
                failures.append(desc)
    finally:
        SRC.write_text(original, encoding="utf-8", newline="\n")
    if failures:
        print(f"\n{len(failures)} 条变异没被抓到：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\n全部 {len(MUTATIONS)} 条变异都被抓到。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
