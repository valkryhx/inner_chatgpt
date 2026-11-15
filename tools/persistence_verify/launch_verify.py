"""今天全量验证用的后端启动器（2026-11-14）。

要同时打开三件今天动过的东西，才能一次覆盖全部改动：

| 环境变量 | 值 | 为什么 |
|---|---|---|
| `HLZ_MEMORY_SELECTOR` | 1 | M3.5 预取 —— 只有开了，`keyword_injection_enabled()` 才会
                          让位，才能验「预取吃档位闸」那条修法 |
| `HLZ_MEMORY_SELECTOR_MODEL` | 湛卢 | 不填则 `prefetch_enabled()` 恒 False，等于没测 |
| `HLZ_MEMORY_AUTO_WRITE` | 1 | M5 的写侧闸 —— 只有提取本来会跑，「只读档没写」才有意义 |
| `HLZ_MEMORY_EVERY_N_TURNS` | 1 | 让每轮都尝试提取，缩短验证时间 |
| `INNER_A2A_ENABLED` | true | A2A 面全部场景的前提 |

为什么用 Python 设 os.environ 再 uvicorn.run：shell 环境变量传到这个进程
在本机不可靠（见 M3.5 实施记录 §5 的排查记录）。
"""
import os
import sys
from pathlib import Path

# tools/persistence_verify/ 下：parent=persistence_verify, parent.parent=tools, 再上一级=仓库根
ROOT = Path(__file__).resolve().parents[2]

os.environ["HLZ_MEMORY_ENABLED"] = "1"
os.environ["HLZ_MEMORY_SELECTOR"] = "1"
os.environ["HLZ_MEMORY_SELECTOR_MODEL"] = "moma/deepseek-latest-flash"
os.environ["HLZ_MEMORY_SELECTOR_MAX_CALLS"] = "3"
os.environ["HLZ_MEMORY_SELECTOR_TIMEOUT_S"] = "8"
os.environ["HLZ_MEMORY_AUTO_WRITE"] = "1"
os.environ["HLZ_MEMORY_EVERY_N_TURNS"] = "1"
# dream 关掉：它会改记忆，会污染下面「记忆零变化」这类断言
os.environ["HLZ_MEMORY_DREAM_ENABLED"] = "0"
os.environ["INNER_A2A_ENABLED"] = "true"
os.environ["INNER_A2A_PUBLIC_BASE_URL"] = "http://127.0.0.1:8000"
# 同步窗口保持默认 60s —— A2A 长任务语义要按**生产默认**验，不能偷偷调松
os.environ.pop("INNER_A2A_SYNC_TIMEOUT_S", None)
# 重放缓冲压到 20 帧：让「溢出降级 sync」在真机上确定性触发。
# 生产默认是 2000 帧 / 2MB，正常一轮几百帧挤不爆，那样这条路径永远验不到。
os.environ["HLZ_TURNJOB_REPLAY_FRAMES"] = "20"

sys.path.insert(0, str(ROOT / "backend"))
os.chdir(str(ROOT / "backend"))
print("=== 今日全量验证启动器 ===", flush=True)
for k in ("HLZ_MEMORY_SELECTOR", "HLZ_MEMORY_SELECTOR_MODEL", "HLZ_MEMORY_AUTO_WRITE",
          "HLZ_MEMORY_DREAM_ENABLED", "INNER_A2A_ENABLED"):
    print(f"  {k}={os.environ.get(k)}", flush=True)

import uvicorn  # noqa: E402

uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False, log_level="info")
