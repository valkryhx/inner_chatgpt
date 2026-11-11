"""L3 攻击性测试：服务端返回异常数据时，搜索面板会不会把整个应用搞白屏。

项目没有任何 ErrorBoundary，所以任何渲染期异常都等于白屏。
这个脚本用 Playwright 的路由拦截伪造各种畸形响应，逐一验证。
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
WORK = ROOT / "work" / "attack_db"
SHOT = ROOT / "work" / "attack_shots"
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
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")


# 畸形的搜索响应 —— 每一种都可能击穿前端
MALFORMED = {
    "hits 不是数组": {"query": "x", "hits": None, "has_more": False, "elapsed_ms": 1},
    "hits 是对象": {"query": "x", "hits": {"a": 1}, "has_more": False, "elapsed_ms": 1},
    "hits 元素缺字段": {"query": "x", "has_more": False, "elapsed_ms": 1,
                    "hits": [{"message_id": "m1"}]},
    "snippet 为 null": {"query": "x", "has_more": False, "elapsed_ms": 1,
                        "hits": [{"message_id": "m1", "session_id": "s1",
                                  "session_title": "t", "role": "user",
                                  "snippet": None, "match_offset": 0,
                                  "match_length": 3, "created_at": 1}]},
    "offset 越界": {"query": "x", "has_more": False, "elapsed_ms": 1,
                  "hits": [{"message_id": "m1", "session_id": "s1",
                            "session_title": "t", "role": "user",
                            "snippet": "短", "match_offset": 999,
                            "match_length": 50, "created_at": 1}]},
    "offset 负数但 length 正": {"query": "x", "has_more": False, "elapsed_ms": 1,
                          "hits": [{"message_id": "m1", "session_id": "s1",
                                    "session_title": "t", "role": "user",
                                    "snippet": "abc", "match_offset": -5,
                                    "match_length": 3, "created_at": 1}]},
    "整个响应是 null": None,
    "hits 里含 null 元素": {"query": "x", "has_more": False, "elapsed_ms": 1,
                       "hits": [None]},
    "role 是未知值": {"query": "x", "has_more": False, "elapsed_ms": 1,
                   "hits": [{"message_id": "m1", "session_id": "s1",
                             "session_title": "t", "role": "hacker",
                             "snippet": "abc", "match_offset": 0,
                             "match_length": 1, "created_at": 1}]},
    "created_at 是字符串": {"query": "x", "has_more": False, "elapsed_ms": 1,
                       "hits": [{"message_id": "m1", "session_id": "s1",
                                 "session_title": "t", "role": "user",
                                 "snippet": "abc", "match_offset": 0,
                                 "match_length": 1, "created_at": "不是数字"}]},
}


def app_is_alive(page) -> dict:
    """判断应用是否还活着（没白屏）。"""
    return page.evaluate("""() => {
        const body = document.body;
        const app = document.querySelector('.app');
        const sidebar = document.querySelector('.sidebar');
        const composer = document.querySelector('.composer');
        const text = (body.innerText || '').trim();
        const visibleEls = [...document.querySelectorAll('button,input,a')]
            .filter(e => e.offsetParent !== null).length;
        return {
            has_app: !!app,
            has_sidebar: !!sidebar,
            has_composer: !!composer,
            text_len: text.length,
            visible_interactive: visibleEls,
            body_children: body.children.length,
        };
    }""")


def main():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True, exist_ok=True)
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
            print(log.name, "启动失败")
            print((WORK / "server.log").read_text(encoding="utf-8", errors="replace")[-2000:])
            return 1
        print(f"后端 {base} 就绪")

        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        email = f"atk_{suffix}@example.invalid"
        pw = "Atk#12345"
        tok = call(base, "/api/auth/register",
                   {"email": email, "password": pw, "name": "攻击测试"})["access_token"]
        s = call(base, "/api/sessions", {"title": "攻击测试会话", "model_id": ""}, token=tok)
        con = sqlite3.connect(str(dbpath))
        now = int(time.time() * 1000)
        con.execute(
            "INSERT INTO messages (id,session_id,role,content,reasoning,blocks,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"m_{suffix}", s["id"], "user", "攻击关键词 GOATX 在这里", "", "[]", "complete", now),
        )
        con.commit()
        con.close()
        print(f"账号 {email} 就绪\n")

        SHOT.mkdir(parents=True, exist_ok=True)
        return run_attacks(base, email, pw, SHOT, s["id"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def run_attacks(base, email, pw, shots, session_id):
    from playwright.sync_api import sync_playwright

    failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})

        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

        # 登录
        page.goto(base, wait_until="networkidle")
        inputs = page.locator("input:visible")
        inputs.nth(0).fill(email)
        inputs.nth(1).fill(pw)
        page.keyboard.press("Enter")
        page.wait_for_timeout(2800)

        baseline = app_is_alive(page)
        print(f"登录后基线: {baseline}")
        if not baseline["has_app"]:
            return 1
        page.screenshot(path=str(shots / "baseline.png"))

        for i, (label, payload) in enumerate(MALFORMED.items()):
            # 每个用例都从干净页面开始，避免互相污染
            page2 = ctx.new_page()
            e2 = []
            page2.on("pageerror", lambda e: e2.append(str(e)))

            # Playwright 的 route handler 会传 (route, request) 两个参数，
            # 用闭包捕获 payload 而不是默认参数，否则会被 request 覆盖。
            def make_handler(payload):
                def handler(route, request):
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(payload),
                    )
                return handler

            page2.route("**/api/sessions/search*", make_handler(payload))
            page2.goto(base, wait_until="networkidle")
            page2.wait_for_timeout(1200)

            # 注入 token 后直接用快捷键开搜索
            page2.evaluate(
                "(t) => localStorage.setItem('app_token', t)",
                page2.evaluate("() => localStorage.getItem('app_token')"),
            )
            page2.keyboard.press("Control+Shift+K")
            page2.wait_for_timeout(500)
            opened = page2.locator(".ss-panel").count() > 0
            if opened:
                page2.fill(".ss-input-row input", "GOATX")
                page2.wait_for_timeout(900)

            state = app_is_alive(page2)
            alive = state["has_app"] and state["text_len"] > 20
            status = "OK  " if alive else "白屏"
            print(f"  {status} {label:22s} app={state['has_app']} "
                  f"text={state['text_len']:5d} 交互元素={state['visible_interactive']}")
            if not alive:
                failures.append((label, state, e2[:2]))
                page2.screenshot(path=str(shots / f"white_{i}_{label[:8]}.png"))

            # 关掉这个页面
            page2.close()

        # 收尾：确认原页面仍然正常
        final = app_is_alive(page)
        print(f"\n收尾基线: {final}")
        page.screenshot(path=str(shots / "final.png"))
        browser.close()

    print("\n" + "=" * 60)
    if failures:
        print(f"发现 {len(failures)} 个白屏场景：")
        for label, state, e in failures:
            print(f"  - {label}: app={state['has_app']} text={state['text_len']} err={e}")
        return 1
    print("所有畸形响应下应用均存活，未出现白屏")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())