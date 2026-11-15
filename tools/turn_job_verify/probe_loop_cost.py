"""AgentLoop 构造耗时（回答「为何必须每轮新建」）。

## 背景

`AgentLoop` 持有 `history`、注入器槽位、「本轮」预算等**随轮次变化**的状态，
所以 `subagent/session.py` 与 `routers/chat.py` 每轮都新建一个实例而不是复用。
复用的风险：上一轮的 `_memory_prefetch_state` / 注入器闭包会跨代残留
（2026-11-14 的 steer 跨代失效缺陷正是这类）。

**新建的前提是够便宜**。本探针实测这个前提是否成立。

## 跑法

    python tools/turn_job_verify/probe_loop_cost.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# tools/turn_job_verify/ 下：parents[2] 是仓库根
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["HLZ_MEMORY_SELECTOR"] = "0"


async def main() -> None:
    from app.agent_loop import AgentLoop
    from app.llm_client import ChatResult
    from app.tools import ToolRegistry

    class _FakeModel:
        async def run(self, *a, **kw):
            return ChatResult(content="")

    registry = ToolRegistry([])
    n = 1000
    t0 = time.monotonic()
    for _ in range(n):
        AgentLoop(_FakeModel(), registry, "sys", max_turns=300, user_id="u")
    per_us = (time.monotonic() - t0) / n * 1_000_000
    print(f"AgentLoop 构造：{per_us:.1f} µs/次（{n} 次平均）")
    print()
    if per_us < 500:
        print(f"→ 每轮新建的成本可忽略（{per_us:.0f}µs），"
              "用新建换「状态不跨轮残留」是划算的。")
    else:
        print(f"→ {per_us:.0f}µs 偏高，需复核构造函数是否引入了重活。")


if __name__ == "__main__":
    asyncio.run(main())
