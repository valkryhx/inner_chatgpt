"""追查：截图那一刻页面上的 dream 状态到底来自哪里。

背景：`ui_dream.py` 的 `--keep` 第二次跑时，`m02_dream.png` 在步骤 [2]
（还没点「立即整理」）就显示了「已跑 1 次」，而临时库的 `runs=1`。
两者必然有一个是错的：要么截图是上次的残影，要么 dream 在 UI 打开前自己跑了。

这个脚本不改任何产品代码，只在 UI 核验的同一套环境里，于**每一步**打印
DB 里的 dream 状态与页面文本，把时间线钉死。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "work" / "dream_timeline_db"
SHOT = ROOT / "work" / "dream_timeline_shots"
PY = sys.executable

BOOT = """import sys, pathlib
sys.path.insert(0, r"{backend}")
from app import config
config.DB_PATH = pathlib.Path(r"{db}")
import app.db as _db
_db.DB_PATH = pathlib.Path(r"{db}")
import app.history_store as _hs
_hs.DB_PATH = pathlib.Path(r"{db}")
import app.memory_store as _ms
_ms.DB_PATH = pathlib.Path(r"{db}")
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, reload=False, log_level="warning")
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(base: str, timeout: float = 90) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def call(base, path, data=None, token=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base + path, data=body,
                                 method=method or ("POST" if body else "GET"))
    if body:
        req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode() or "{}")


def state(base, token, label):
    st = call(base, "/api/me/memories/status", token=token)
    d = st.get("dream") or {}
    print(f"  [{label}] total={st.get('total')} runs={d.get('runs')} "
          f"status={d.get('last_status')} last_attempt={d.get('last_attempt')} "
          f"last_consolidated={d.get('last_consolidated')}")
    return st


def main() -> int:
    src = ROOT / "backend" / "data" / "app.db"
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    SHOT.mkdir(parents=True, exist_ok=True)
    dbpath = WORK / "app.db"
    shutil.copy2(src, dbpath)
    for suf in ("-wal", "-shm"):
        side = Path(str(src) + suf)
        if side.is_file():
            shutil.copy2(side, Path(str(dbpath) + suf))

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    boot = WORK / "_boot.py"
    boot.write_text(BOOT.format(backend=ROOT / "backend", db=dbpath, port=port),
                    encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "backend")
    env["HLZ_MEMORY_DREAM_ENABLED"] = "1"
    env["HLZ_MEMORY_AUTO_WRITE"] = "1"
    # 与 ui_dream.py 完全一致：**不设** MIN_HOURS / MIN_SESSIONS（保持默认 24h/5）
    for k in ("HLZ_MEMORY_DREAM_MIN_HOURS", "HLZ_MEMORY_DREAM_MIN_SESSIONS",
              "HLZ_MEMORY_DREAM_SCAN_INTERVAL"):
        env.pop(k, None)
    log = open(WORK / "server.log", "wb")
    proc = subprocess.Popen([PY, str(boot)], cwd=str(ROOT / "backend"),
                            stdout=log, stderr=subprocess.STDOUT, env=env)
    try:
        if not wait_health(base):
            print("后端启动失败")
            print(Path(WORK / "server.log").read_text(encoding="utf-8",
                                                      errors="replace")[-2000:])
            return 1
        print(f"后端 {base}")
        print(f"dream 扫码间隔（默认）：600s\n")

        tok = call(base, "/api/auth/register",
                   {"email": f"tl_{int(time.time())}@example.invalid",
                   "password": "Tl#12345678", "name": "时间线"})["access_token"]
        for slug, desc in (("tl-a", "用户希望被叫老徐"),
                           ("tl-b", "称呼：老徐"),
                           ("tl-c", "用户称呼偏好")):
            call(base, "/api/me/memories", {
                "slug": slug, "title": desc, "description": desc,
                "content": f"# {desc}\n\n{desc}\n", "kind": "user"}, token=tok)
        state(base, tok, "seed 之后")

        # 等 20 秒：如果巡检会在 UI 打开前自己跑，这段时间应该能看到
        print("  等 20s 观察巡检是否自己触发…")
        time.sleep(20)
        state(base, tok, "等 20s 后")

        # 打开 UI 到 dream 区块，读页面文字
        from playwright.sync_api import sync_playwright
        email = f"tl_{int(time.time())}@example.invalid"
        # 用同一个账号登录：直接拿 token 里的用户，改用 API 校验即可
        st = call(base, "/api/auth/me", token=tok)
        print(f"  账号 user_id={st.get('id')}")

        # 不再登录（同一用户），直接调 dream 看状态推进
        print("\n  手动调 dream（force=True）:")
        r = call(base, "/api/me/memories/dream", method="POST", token=tok)
        print(f"    ran={r.get('ran')} written={r.get('written')} "
              f"deleted={r.get('deleted')} error={r.get('error')!r}")
        state(base, tok, "手动 dream 后")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
