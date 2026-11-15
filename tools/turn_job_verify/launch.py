"""真机自测启动器（2026-11-15）：覆盖持久化第三步阶段 1~4 的全部改动。

## 为什么要一个专门的启动器

本轮动的是 **SSE 帧层**（心跳、seq 重写、重放缓冲、sync 降级）与 **job 生命周期**
（解耦、代际栅栏、未启动即取消的收口）。这些路径只有在特定环境变量下才会被
走到或才容易观测：

| 环境变量 | 值 | 原因 |
|---|---|---|
| `HLZ_TURNJOB_HEARTBEAT_IDLE_S` | `4` | 生产默认 15s，一轮几百帧里静默窗口不够长。压到 4s 让「静默期心跳」在真机（含 `code_run` 长空档）确定性地出现多次 |
| `HLZ_TURNJOB_REPLAY_FRAMES` | `60` | 生产默认 2000 帧，正常一轮几百帧永远挤不爆，「缓冲溢出 → 降级 sync」这条分支永远验不到。压到 60 让它必然触发 |
| `INNER_A2A_ENABLED` | `true` | A2A 面要验：它必须仍能直接吃 `_event_stream`（本轮给它加了可选 `job` 参数） |
| `INNER_A2A_PUBLIC_BASE_URL` | 本机 | 卡片 URL 用 |
| `HLZ_LOG_LEVEL` / `HLZ_PRELUDE_LOG` | info / 1 | 要能从后端日志里读到事务与首帧计时 |

**刻意保持生产默认的项**（改了就等于没验）：

- `INNER_A2A_SYNC_TIMEOUT_S` —— 不设，保持 60s 默认。A2A 长任务语义要按生产值验。
- `HLZ_MEMORY_SELECTOR` / `HLZ_MEMORY_AUTO_WRITE` —— 不设。记忆与本轮改动无关，
  开着只会引入额外的模型调用、拖慢每一轮，并且污染「一轮跑多久」的观测。

## 数据库隔离（**默认就隔离**，这是血泪教训）

第一版**没有**隔离 DB，`live_selftest` 直接写进了生产库 `backend/data/app.db`：
一次自测留下 16 个测试账号 / 91 个会话 / 176 条消息，A2A 回归又添 15 个。
虽然事先备份了，但**备份不等于没污染** —— 事后要逐条清理，还得确认清理干净。

现在默认把 DB 复制到 `work/turnjob_verify/app.db` 再跑（`sqlite3.backup`，
WAL 安全），生产库一个字节都不碰。要打生产库请显式加 `--use-prod-db`
（几乎不该用，除非你在验迁移）。

⚠️ 不存在 `HLZ_DB_PATH` 这个环境变量 —— 复制 `tools/turn_job_verify/ui_resume.py`
的 BOOT 做法，逐模块改 `DB_PATH`（`config` / `db` / `history_store` / `memory_store`
/ `team_store` / `subagent.store` / `a2a.store` / `turn_marker` 各有自己的常量）。

## 为什么不直接用 shell 环境变量

本机实测：shell 里 export 的变量传到 `uvicorn.run` 所在进程不可靠（见记忆系统
M3.5 实施记录 §5）。统一在 Python 里 `os.environ[...] = ...` 再 `uvicorn.run`。

## 跑法

    python tools/turn_job_verify/launch.py            # 隔离库，前台，Ctrl-C 退出
    python tools/turn_job_verify/launch.py --port 8100
    python tools/turn_job_verify/launch.py --use-prod-db   # 慎用
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "work" / "turnjob_verify"
SRC_DB = ROOT / "backend" / "data" / "app.db"
DST_DB = LOG_DIR / "app.db"


def prepare_db() -> Path:
    """把生产库复制一份到 work/，返回副本路径。

    用 `sqlite3.backup` 而不是复制文件：源库有 WAL，直接拷会缺内容。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(DST_DB) + suffix)
        if f.exists():
            f.unlink()
    if SRC_DB.exists():
        with sqlite3.connect(str(SRC_DB)) as s, sqlite3.connect(str(DST_DB)) as d:
            s.backup(d)
    return DST_DB


def main(argv: list[str]) -> int:
    port = 8000
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    use_prod = "--use-prod-db" in argv

    os.environ["HLZ_TURNJOB_HEARTBEAT_IDLE_S"] = "4"
    os.environ["HLZ_TURNJOB_REPLAY_FRAMES"] = "60"
    os.environ["INNER_A2A_ENABLED"] = "true"
    os.environ.setdefault("INNER_A2A_PUBLIC_BASE_URL", f"http://127.0.0.1:{port}")
    os.environ["HLZ_LOG_LEVEL"] = "info"
    os.environ["HLZ_PRELUDE_LOG"] = "1"
    # 生产默认，不要动（写在这里是为了显式表达「不碰」）
    os.environ.pop("INNER_A2A_SYNC_TIMEOUT_S", None)

    if use_prod:
        db_note = "**生产库**（--use-prod-db）"
        boot = "import uvicorn\nuvicorn.run('app.main:app', host='127.0.0.1', port={port}, reload=False, log_level='info')"
    else:
        db = prepare_db()
        db_note = str(db)
        boot = """import sys, pathlib
sys.path.insert(0, r"{backend}")
from app import config
config.DB_PATH = pathlib.Path(r"{db}")
import app.db as _db
_db.DB_PATH = pathlib.Path(r"{db}")
import app.history_store as _hs
_hs.DB_PATH = pathlib.Path(r"{db}")
import app.memory_store as _ms
_ms.DB_PATH = pathlib.Path(r"{db}")
import app.team_store as _ts
_ts.DB_PATH = pathlib.Path(r"{db}")
import app.subagent.store as _ss
_ss.DB_PATH = pathlib.Path(r"{db}")
import app.a2a.store as _as
_as.DB_PATH = pathlib.Path(r"{db}")
import app.turn_marker as _tm
_tm.DB_PATH = pathlib.Path(r"{db}")
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, reload=False, log_level="info")
"""

    sys.path.insert(0, str(ROOT / "backend"))
    os.chdir(str(ROOT / "backend"))

    print("=" * 72, flush=True)
    print(f"真机自测启动器  port={port}", flush=True)
    print(f"  数据库      = {db_note}", flush=True)
    for k in ("HLZ_TURNJOB_HEARTBEAT_IDLE_S", "HLZ_TURNJOB_REPLAY_FRAMES",
              "INNER_A2A_ENABLED", "INNER_A2A_PUBLIC_BASE_URL", "HLZ_LOG_LEVEL"):
        print(f"  {k}={os.environ.get(k)}", flush=True)
    print("=" * 72, flush=True)

    code = boot.format(backend=str(ROOT / "backend"), db=str(DST_DB), port=port)
    exec(compile(code, "<launcher-boot>", "exec"), {"__name__": "__main__"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
