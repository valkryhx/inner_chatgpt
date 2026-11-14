"""M3.5 预取的反向确认（变异测试）。

把预取的每个关键机制逐个改坏，看对应测试是否变红。全绿 = 测试没测到东西。

跑法::

    cd backend && python ../tools/memory_verify/prefetch_mutation.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
SRC = BASE / "backend" / "app" / "memory_prefetch.py"
LOOP = BASE / "backend" / "app" / "agent_loop.py"
INJECT = BASE / "backend" / "app" / "memory_inject.py"
BACKEND = BASE / "backend"

# (说明, 文件, 原文, 替换为, 应红的测试)
MUTATIONS = [
    (
        "启动就 await 选择器（退化成同步，拖慢首帧）",
        SRC,
        "        self.promise = asyncio.ensure_future(coro)",
        "        self.promise = asyncio.ensure_future(coro)\n"
        "        import time as _t; _t.sleep(0.3)  # 模拟同步等待",
        "test_m15_prefetch_does_not_block_first_token",
    ),
    (
        "未 settle 也允许消费（首轮就注入，等于同步）",
        SRC,
        "        if self.settled_at is None or self.consumed_on_iteration != -1:",
        "        if self.consumed_on_iteration != -1:",
        "test_m16_take_before_settle_returns_empty",
    ),
    (
        "允许重复消费（同一句柄注入两次）",
        SRC,
        "        if self.settled_at is None or self.consumed_on_iteration != -1:",
        "        if self.settled_at is None:",
        "test_m16_consumed_once_then_never_again",
    ),
    (
        "不按 slug 去重（每轮重复注入）",
        SRC,
        "    fresh = [by_slug[s] for s in picked_slugs\n"
        "             if s in by_slug and s not in injected]",
        "    fresh = [by_slug[s] for s in picked_slugs if s in by_slug]",
        "test_slug_dedup_across_calls",
    ),
    (
        "去掉选择器调用次数上限（成本闸失效）",
        SRC,
        "    if st is not None and st.calls >= max_calls_per_generation():",
        "    if False:",
        "test_max_calls_per_generation",
    ),
    (
        "去掉防自激闸（选择器可能递归触发自己）",
        SRC,
        "    if query_source in NO_SELF_TRIGGER:      # 防自激\n        return None",
        "    if False:\n        return None",
        "test_no_self_trigger_for_known_sources",
    ),
    (
        "不过滤模型编造的 slug（读侧幽灵引用）",
        SRC,
        "        if slug in allowed_set and slug not in out:",
        "        if slug not in out:",
        "test_parse_filters_unknown_slugs",
    ),
    (
        "会话预算不生效（越聊越多）",
        SRC,
        "    def exhausted(self) -> bool:\n"
        "        return self.limit > 0 and self.used >= self.limit",
        "    def exhausted(self) -> bool:\n        return False",
        "test_session_budget_blocks_further_recall",
    ),
    (
        "注入块不带 _volatile（会落进 session_history）",
        SRC,
        '    return [{"role": "user", "content": text, VOLATILE_KEY: True}]',
        '    return [{"role": "user", "content": text}]',
        "test_selected_block_has_disclaimer",
    ),
    (
        "没指定模型也启用（成本翻倍）",
        SRC,
        "            and selector_enabled() and bool(selector_model()))",
        "            and selector_enabled())",
        "test_enabled_requires_model",
    ),
    (
        "共享去重集合不传给预取（两条路径各记一份 -> 重复注入）",
        SRC,
        "        shared = getattr(loop, \"_memory_injected_slugs\", None)",
        "        shared = None",
        "test_factory_wires_shared_set_from_loop",
    ),
    (
        "两条路径不互斥（关键词先抢 slug，选择器白跑）",
        INJECT,
        "    if not keyword_injection_enabled():",
        "    if False:",
        "test_selector_path_injects_when_keyword_yields",
    ),
    (
        "轮循环里不消费预取（第一轮之后永远拿不到）",
        LOOP,
        "            for msg in await self._take_prefetch(turn):\n"
        "                history.append(msg)\n"
        '                yield {"type": "message_injected", "text": msg.get("content", "")}',
        "            pass",
        "test_loop_consumes_only_after_tool_steps",
    ),
]


def run_test(name: str) -> bool:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", f"tests/test_memory_prefetch.py::{name}",
         "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=BACKEND, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    return r.returncode == 0


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8") for p in (SRC, LOOP, INJECT)}
    failures: list[str] = []
    try:
        if not run_test("test_selector_off_by_default"):
            print("[FAIL] 基线就是红的，先修测试")
            return 1
        for desc, path, old, new, test_name in MUTATIONS:
            src = originals[path]
            if old not in src:
                print(f"[SKIP] 原文没找到：{desc}")
                failures.append(f"{desc}（原文不匹配）")
                continue
            path.write_text(src.replace(old, new, 1), encoding="utf-8", newline="\n")
            passed = run_test(test_name)
            path.write_text(originals[path], encoding="utf-8", newline="\n")
            print(f"[{'FAIL' if passed else 'OK  '}] {desc} -> {test_name} "
                  f"{'仍然通过（没测到）' if passed else '变红'}")
            if passed:
                failures.append(desc)
    finally:
        for p, text in originals.items():
            p.write_text(text, encoding="utf-8", newline="\n")
    if failures:
        print(f"\n{len(failures)} 条没被抓到：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\n全部 {len(MUTATIONS)} 条都被抓到。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
