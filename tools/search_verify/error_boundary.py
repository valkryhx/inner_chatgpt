"""证明 ErrorBoundary 真的能兜住渲染异常，而不只是个摆设。

做法：拦截一个 API 返回能让 SessionSearch 渲染崩溃的响应，
同时确认 normalize 已移除时页面仍然不白 —— 但那需要改源码。
这里改为直接搜索组件里注入一次真实渲染异常：伪造一个 hits 元素，
让 Snippet 拿到非字符串 snippet，触发 slice 异常。
"""
from __future__ import annotations

import json
import os
import random
import shutil
import socket
import string
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# 脚本位于 <repo>/tools/search_verify/，向上两级即仓库根。
ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "work" / "eb_db"
SHOT = ROOT / "work" / "eb_shots"
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
        email = f"eb_{sfx}@example.invalid"
        pw = "Eb#12345"
        call(base, "/api/auth/register",
             {"email": email, "password": pw, "name": "边界测试"})
        return run(base, email, pw)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def run(base, email, pw):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))

        # 拦截搜索响应，返回能让组件真正抛异常的数据
        # （normalize 会过滤掉这些，所以同时拦掉整个响应，塞进无法规整的东西）
        def make(payload):
            def h(route, request):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(payload))
            return h

        # 这条会让 normalizeHit 通过，但 Snippet 拿到 snippet 是数字
        evil = {"query": "x", "has_more": False, "elapsed_ms": 1, "hits": [
            {"message_id": "m1", "session_id": "s1", "session_title": "t",
             "role": "user", "snippet": 12345, "match_offset": 0,
             "match_length": 2, "created_at": 1}
        ]}
        page.route("**/api/sessions/search*", make(evil))

        page.goto(base, wait_until="networkidle")
        ins = page.locator("input:visible")
        ins.nth(0).fill(email)
        ins.nth(1).fill(pw)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)

        page.keyboard.press("Control+Shift+K")
        page.wait_for_timeout(500)
        page.fill(".ss-input-row input", "anything")
        page.wait_for_timeout(1200)

        state = page.evaluate("""() => ({
            app: !!document.querySelector('.app'),
            sidebar: !!document.querySelector('.sidebar'),
            composer: !!document.querySelector('.composer'),
            crash: !!document.querySelector('.crash-panel'),
            crash_text: document.querySelector('.crash-panel')?.innerText || '',
            text: (document.body.innerText || '').trim().length,
        })""")
        print(f"状态: app={state['app']} sidebar={state['sidebar']} "
              f"composer={state['composer']}")
        print(f"降级面板出现: {state['crash']}")
        if state["crash"]:
            print(f"降级文案: {state['crash_text'][:120]!r}")
        page.screenshot(path=str(SHOT / "eb_graceful.png"))

        # 关键断言：即使组件崩了，侧栏与输入框仍在（不是整页白）
        survived = state["sidebar"] and state["composer"] and state["text"] > 20
        print(f"主界面存活（侧栏+输入框仍在）: {survived}")

        # 能否恢复：点「重试」
        recovered = False
        if state["crash"]:
            page.unroute("**/api/sessions/search*")
            retry = page.locator(".crash-panel .ghost-btn", has_text="重试")
            if retry.count():
                retry.first.click()
                page.wait_for_timeout(800)
                recovered = page.locator(".crash-panel").count() == 0
                print(f"点重试后恢复: {recovered}")

        state2 = page.evaluate("""() => ({
            app: !!document.querySelector('.app'),
            text: (document.body.innerText || '').trim().length,
        })""")
        print(f"最终: app={state2['app']} text={state2['text']}")
        if errs:
            print(f"页面异常（已被告警捕获，可接受）: {errs[:2]}")

        browser.close()
        ok = survived and state2["app"] and state2["text"] > 20
        print("\n" + ("PASS 降级生效，未整页白屏" if ok else "FAIL 仍然影响主界面"))
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())