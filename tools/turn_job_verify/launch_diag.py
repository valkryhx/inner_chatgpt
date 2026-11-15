"""带诊断的启动器：隔离 DB + 包装 `has_active_generation` 打印判据细节。

## 为什么不能只看代码

`active_probe.py` 用真的 `turn_job` + 真的 `runtime` 在进程内复现，**全部通过**
（静默 40 秒里判据一直是 True）。但真机 `stale_probe.py` 在 58.7s 观测到
`stopped` —— 那时那一轮还在出帧，判据必然为 False。

两条路都"看起来对"，说明真机路径里有进程内探针没覆盖的东西（真实的 `chat.py`
`_event_stream` / `_run_job` / 异步节流 persist 的组合）。**这时候不能再推**，
要拿现场数据。

## 做法（不修改任何源码文件）

启动时用 `python -c` 包一层：导入 `app.runtime` 后**运行时替换**
`has_active_generation`，把每次调用的三个注册表内容与判定结果打印到 stderr。
然后 uvicorn 照常启动。

- `_stop_events` / `_generation_tasks` / `_generation_reservations` 的键与内容；
- 每个键 `_is_live_key()` 的真值；
- 传入的 `session_id` / `user_id`；
- 最终返回值。

## 跑法

    python tools/turn_job_verify/launch_diag.py --port 8101

然后另开一个终端打它：

    python tools/turn_job_verify/stale_probe.py --base http://127.0.0.1:8101

诊断行以 `[ACTIVE]` 为前缀，落在启动器的 stdout（重定向到日志即可）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "work" / "turnjob_diag"
SRC_DB = ROOT / "backend" / "data" / "app.db"
DST_DB = LOG_DIR / "app.db"


def prepare_db() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(DST_DB) + suffix)
        if f.exists():
            f.unlink()
    if SRC_DB.exists():
        with sqlite3.connect(str(SRC_DB)) as s, sqlite3.connect(str(DST_DB)) as d:
            s.backup(d)
    return DST_DB


BOOT = """import sys, pathlib, time, os
sys.path.insert(0, r"{backend}")
{db_patch}

# ---- 诊断包装：只读、不改行为 ----
import app.runtime as _rt
_orig = _rt.has_active_generation

def _spy(session_id, user_id=""):
    res = _orig(session_id, user_id)
    try:
        keys = set(_rt._stop_events) | set(_rt._generation_tasks) | set(_rt._generation_reservations)
        rows = []
        for k in keys:
            owner, sid, gid = k
            if sid != session_id:
                continue
            task = _rt._generation_tasks.get(k)
            rows.append(
                "owner=%s gid=%s stop_ev=%s task=%s done=%s res_age=%.1f live=%s" % (
                    owner, gid, k in _rt._stop_events,
                    task is not None,
                    (task.done() if task is not None else None),
                    (time.monotonic() - _rt._generation_reservations[k]
                     if k in _rt._generation_reservations else -1.0),
                    _rt._is_live_key(k),
                ))
        print("[ACTIVE] sid=%s uid=%s -> %s | keys=%d | %s" % (
            session_id, user_id, res, len(rows), " ; ".join(rows) or "NO-KEYS"),
            file=sys.stderr, flush=True)
    except Exception as e:
        print("[ACTIVE] spy failed: %r" % (e,), file=sys.stderr, flush=True)
    return res

_rt.has_active_generation = _spy

# ---- 写手追踪：谁把 streaming 改成 stopped，直接打调用栈 ----
import traceback as _tb
_orig_mark = _db.mark_stale_streaming_stopped

async def _mark_spy(session_id):
    n = await _orig_mark(session_id)
    if n:
        print("[MARK] mark_stale_streaming_stopped sid=%s rows=%d" % (session_id, n),
              file=sys.stderr, flush=True)
        _tb.print_stack(file=sys.stderr)
        sys.stderr.flush()
    return n

_db.mark_stale_streaming_stopped = _mark_spy

# ---- 写手追踪 ②：所有把消息写成 stopped 的路径（update_message / add_message）----
_orig_update = _db.update_message

async def _update_spy(message_id, **fields):
    if fields.get("status") == "stopped":
        print("[WRITE] update_message id=%s status=stopped" % (message_id,),
              file=sys.stderr, flush=True)
        _tb.print_stack(file=sys.stderr)
        sys.stderr.flush()
    return await _orig_update(message_id, **fields)

_db.update_message = _update_spy

_orig_add = _db.add_message

async def _add_spy(session_id, role, content="", **kw):
    if kw.get("status") == "stopped":
        print("[WRITE] add_message sid=%s role=%s status=stopped" % (session_id, role),
              file=sys.stderr, flush=True)
        _tb.print_stack(file=sys.stderr)
        sys.stderr.flush()
    return await _orig_add(session_id, role, content=content, **kw)

_db.add_message = _add_spy

import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, reload=False, log_level="info")
"""


DB_PATCH = """from app import config
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
_tm.DB_PATH = pathlib.Path(r"{db}")"""


def main(argv: list[str]) -> int:
    port = 8101
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    use_prod = "--use-prod-db" in argv

    os.environ.setdefault("INNER_A2A_ENABLED", "false")
    os.environ["HLZ_LOG_LEVEL"] = "info"
    # 生产默认心跳，不压小（本探针不关心心跳）
    os.environ.pop("HLZ_TURNJOB_HEARTBEAT_IDLE_S", None)

    if use_prod:
        src = str(ROOT / "backend" / "data" / "app.db")
        print("=" * 72, flush=True)
        print(f"诊断启动器  port={port}  db=**生产库**（--use-prod-db，只读追踪，不写测试数据）",
              flush=True)
        print("=" * 72, flush=True)
    else:
        db = prepare_db()
        src = str(db)
        print("=" * 72, flush=True)
        print(f"诊断启动器  port={port}  db={src}", flush=True)
        print("=" * 72, flush=True)

    sys.path.insert(0, str(ROOT / "backend"))
    os.chdir(str(ROOT / "backend"))

    code = BOOT.format(backend=str(ROOT / "backend"),
                       db=src, db_patch=DB_PATCH.format(db=src), port=port)
    exec(compile(code, "<diag-boot>", "exec"), {"__name__": "__main__"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
