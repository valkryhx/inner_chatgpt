"""M5 e2e 专用启动器：自动提取**强制打开**，选择器关闭。

为什么要强制打开 `HLZ_MEMORY_AUTO_WRITE`：
S-M4b 断言「只读 token 跑完一轮后记忆库零变化」。若提取本来就没开，
「没写入」是必然的，测不出任何东西 —— 必须让提取带着跑的意图去跑，
才能证明是**档位闸**拦住了它，而不是「本来就不写」。

选择器关掉是为了排除干扰：它只影响读侧，与 S-M4b 的写侧断言无关，
但开着会让 S-M3 的注入形态变成 `[记忆·相关]`，增加解读成本。

用 os.environ 显式设置再 uvicorn.run —— shell 环境变量传到这个进程不可靠
（见 docs/20261114-记忆系统M3.5选择性召回实施记录.md §5 的排查记录）。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["HLZ_MEMORY_AUTO_WRITE"] = "1"
os.environ["HLZ_MEMORY_EVERY_N_TURNS"] = "1"
os.environ["HLZ_MEMORY_SELECTOR"] = "0"
os.environ["INNER_A2A_ENABLED"] = "true"
os.environ["INNER_A2A_PUBLIC_BASE_URL"] = "http://127.0.0.1:8000"
sys.path.insert(0, str(ROOT / "backend"))
os.chdir(str(ROOT / "backend"))
print("M5 e2e launcher: AUTO_WRITE=", os.environ.get("HLZ_MEMORY_AUTO_WRITE"),
      " SELECTOR=", os.environ.get("HLZ_MEMORY_SELECTOR"), flush=True)

import uvicorn  # noqa: E402

uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False, log_level="info")
