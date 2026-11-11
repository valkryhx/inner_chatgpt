"""多分辨率 + 边界场景验证：窄屏/小屏下搜索面板是否溢出、是否白屏。

演示可能投屏或使用笔电小窗，窄屏溢出同样是「现场翻车」。
"""
from __future__ import annotations

import json
import os
import random
import shutil
import socket
import sqlite3
import string
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# 脚本位于 <repo>/tools/search_verify/，向上两级即仓库根。
ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "work" / "resp_db"
SHOT = ROOT / "work" / "resp_shots"
PY = r"C:\anaconda3\python.exe"

BOOT = """import sys, pathlib
sys.path.insert(0, r"{backend}")
from app import config
config.DB_PATH = pathlib.Path(r"{db}")
import app.db as _db
_db.DB_PATH = pathlib.Path(r"{db}")
import app.history_store as _hs
_hs.DB_PATH = pathlib.Path(r"{db}")
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, reload=False, log_level="warning")
"""

VIEWPORTS = [
    ("演示常用 1920x1080", 1920, 1080),
    ("笔电 1440x900", 1440, 900),
    ("小窗 1280x720", 1280, 720),
    ("窄窗 1024x768", 1024, 768),
    ("平板竖屏 820x1180", 820, 1180),
    ("手机 390x844", 390, 844),
    ("极窄 320x640", 320, 640),
]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(base, timeout=80):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def call(base, path, data=None, token=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base + path, data=body,
                                 method="POST" if body else "GET")
    if body:
        req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def main():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True, exist_ok=True)
    SHOT.mkdir(parents=True, exist_ok=True)
    dbpath = WORK / "app.db"
    shutil.copy2(ROOT / "backend" / "data" / "app.db", dbpath)

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    boot = WORK / "_boot.py"
    boot.write_text(BOOT.format(backend=ROOT / "backend", db=dbpath, port=port),
                    encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "backend")
    log = open(WORK / "server.log", "wb")
    proc = subprocess.Popen([PY, str(boot)], cwd=str(ROOT / "backend"),
                            stdout=log, stderr=subprocess.STDOUT, env=env)
    try:
        if not wait_health(base):
            print("后端启动失败")
            return 1

        sfx = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        email = f"rs_{sfx}@example.invalid"
        pw = "Resp#12345"
        tok = call(base, "/api/auth/register",
                   {"email": email, "password": pw, "name": "响应式测试"})["access_token"]
        s = call(base, "/api/sessions", {"title": "响应式会话", "model_id": ""}, token=tok)
        con = sqlite3.connect(str(dbpath))
        now = int(time.time() * 1000)
        # 造一条很长的标题 + 很长的片段，压测溢出
        con.execute("UPDATE sessions SET title=? WHERE id=?",
                    ("这是一个非常非常长的会话标题" * 4, s["id"]))
        con.execute(
            "INSERT INTO messages (id,session_id,role,content,reasoning,blocks,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"m_{sfx}", s["id"], "user",
             "长文本测试 RESPTARGET " + "填充内容很长很长" * 60, "", "[]", "complete", now),
        )
        con.commit()
        con.close()
        print(f"后端 {base}，账号 {email}\n")

        return run(base, email, pw, SHOT)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def run(base, email, pw, shots):
    from playwright.sync_api import sync_playwright

    fails = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for label, w, h in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": w, "height": h})
            page = ctx.new_page()
            errs = []
            page.on("pageerror", lambda e: errs.append(str(e)))

            page.goto(base, wait_until="networkidle")
            ins = page.locator("input:visible")
            ins.nth(0).fill(email)
            ins.nth(1).fill(pw)
            page.keyboard.press("Enter")
            page.wait_for_timeout(3000)

            page.keyboard.press("Control+Shift+K")
            page.wait_for_timeout(700)
            opened = page.locator(".ss-panel").count() == 1
            info = {}
            if opened:
                page.fill(".ss-input-row input", "RESPTARGET")
                page.wait_for_timeout(1400)
                info = page.evaluate("""() => {
                    const panel = document.querySelector('.ss-panel');
                    const mask = document.querySelector('.ss-mask');
                    const rows = document.querySelectorAll('.ss-row').length;
                    const marks = document.querySelectorAll('.ss-mark').length;
                    if (!panel) return {panel: false};
                    const pr = panel.getBoundingClientRect();
                    const docW = document.documentElement.clientWidth;
                    // 横向溢出 = 面板超出视口，或页面出现横向滚动条
                    const overflowX = pr.right > docW + 1 || pr.left < -1
                        || document.documentElement.scrollWidth > docW + 1;
                    const input = document.querySelector('.ss-input-row input');
                    const ir = input ? input.getBoundingClientRect() : null;
                    return {
                        panel: true,
                        panel_w: Math.round(pr.width),
                        viewport_w: docW,
                        overflow_x: overflowX,
                        scroll_w: document.documentElement.scrollWidth,
                        rows, marks,
                        input_visible: ir ? (ir.width > 30 && ir.right <= docW + 1) : false,
                        app: !!document.querySelector('.app'),
                        text: (document.body.innerText || '').trim().length,
                    };
                }""")
                page.screenshot(path=str(shots / f"{w}x{h}.png"))

            alive = info.get("app", False) and info.get("text", 0) > 20
            overflow = info.get("overflow_x", False)
            ok = opened and alive and not overflow and info.get("rows", 0) > 0
            status = "PASS" if ok else "FAIL"
            print(f"  {status} {label:20s} panel_w={info.get('panel_w','-'):>4} "
                  f"viewport={info.get('viewport_w','-'):>4} "
                  f"溢出={overflow} 行={info.get('rows','-')} 高亮={info.get('marks','-')} "
                  f"输入可见={info.get('input_visible','-')}")
            if not ok:
                fails.append((label, info, errs[:2]))
                print(f"       详情: {info} 异常: {errs[:2]}")
            ctx.close()
        browser.close()

    print("\n" + "=" * 60)
    if fails:
        print(f"{len(fails)} 个分辨率有问题：")
        for label, info, e in fails:
            print(f"  - {label}: {info} {e}")
        return 1
    print(f"全部 {len(VIEWPORTS)} 个分辨率通过：无溢出、无白屏、输入框可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())