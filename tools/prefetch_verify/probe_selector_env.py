"""最轻量的一步：环境变量与 `prefetch_enabled()` 的真实结果。

## 为什么需要它

预取（M3.5 选择器）**默认关闭**，而且有两道独立的闸：

1. `HLZ_MEMORY_SELECTOR` 总开关；
2. `HLZ_MEMORY_SELECTOR_MODEL` —— **不填则 `prefetch_enabled()` 恒 False**。

排查「为什么选择器没跑」时，先跑这个，别急着读代码。

## 跑法

    python tools/prefetch_verify/probe_selector_env.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# tools/prefetch_verify/ 下：parents[2] 是仓库根
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

print("仓库根:", ROOT)
print("env SELECTOR:", os.environ.get("HLZ_MEMORY_SELECTOR"))
print("env MODEL:", os.environ.get("HLZ_MEMORY_SELECTOR_MODEL"))

from app import memory_prefetch as mp  # noqa: E402

print("prefetch_enabled:", mp.prefetch_enabled())
print("selector_model:", repr(mp.selector_model()))
print()
if not mp.prefetch_enabled():
    print("提示：恒为 False 时先确认 ——")
    print("  · HLZ_MEMORY_SELECTOR 是否为 1")
    print("  · HLZ_MEMORY_SELECTOR_MODEL 是否非空（这道闸最容易漏）")
