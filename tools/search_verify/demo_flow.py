"""演示场景全链路：真实 LLM 对话 → 搜索该对话 → 命中并跳转。

这是明天演示最可能的动线。前几步用真实 LLM（不 mock），
搜索段用真实浏览器点真实 UI。
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
WORK = ROOT / "work" / "demo_db"
SHOT = ROOT / "work" / "demo_shots"
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


def wait_health(base, timeout=90):
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
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode() or "{}")


def main():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True, exist_ok=True)
    SHOT.mkdir(parents=True, exist_ok=True)
    dbpath = WORK / "app.db"
    # 从生产库复制，带上真实的模型配置
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
            print((WORK / "server.log").read_text(encoding="utf-8", errors="replace")[-2000:])
            return 1
        print(f"后端 {base}\n")

        sfx = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        email = f"demo_{sfx}@example.invalid"
        pw = "Demo#12345"
        tok = call(base, "/api/auth/register",
                   {"email": email, "password": pw, "name": "演示账号"})["access_token"]
        print(f"账号 {email}")

        # 关键：用真实 LLM 生成一段对话（走 /api/sessions/{id}/stream 或 chat 端点）
        sess = call(base, "/api/sessions", {"title": "演示对话", "model_id": ""}, token=tok)
        sid = sess["id"]
        sentinel = f"DEMO{sfx.upper()}SENTINEL"
        prompt = (f"请只回答一句话：本次验证口令是 {sentinel}，请原样复述它。"
                  f"不要调用任何工具。")
        print(f"发送真实 LLM 请求（哨兵 {sentinel}）…")

        reply = stream_chat(base, sid, prompt, tok)
        # SSE 字段名不该靠猜：以落库结果为准（真实链路证明）。
        import sqlite3
        con = sqlite3.connect(str(dbpath))
        row = con.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='assistant' "
            "ORDER BY created_at DESC LIMIT 1", (sid,)
        ).fetchone()
        con.close()
        stored = (row[0] if row else "") or ""
        print(f"模型回复（落库，前 200 字）：{stored[:200]!r}")
        has_sentinel = sentinel in stored
        print(f"哨兵复述: {has_sentinel}\n")

        # 用搜索验证刚生成的对话能被搜到
        hits = call(base, f"/api/sessions/search?q={sentinel}", token=tok)
        print(f"搜索哨兵 -> {len(hits.get('hits', []))} 条, {hits.get('elapsed_ms')}ms")
        for h in hits.get("hits", [])[:3]:
            print(f"  [{h['role']}] {h['session_title']}: {h['snippet'][:80]}")

        # 浏览器：走完演示动线
        ok = run_demo_ui(base, email, pw, sentinel, SHOT)
        return 0 if (has_sentinel and hits.get("hits") and ok) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def stream_chat(base, sid, prompt, token):
    """调真实 SSE 流，拼接出 assistant 的完整文本。"""
    import http.client
    from urllib.parse import urlparse

    u = urlparse(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=200)
    body = json.dumps({"text": prompt, "mode": "send"}).encode()
    conn.request("POST", f"/api/sessions/{sid}/messages", body=body,
                 headers={"content-type": "application/json",
                          "authorization": "Bearer " + token})
    resp = conn.getresponse()
    print(f"  SSE status={resp.status}")
    if resp.status != 200:
        print("  ", resp.read()[:500])
        return ""
    text_parts = []
    while True:
        line = resp.readline()
        if not line:
            break
        s = line.decode("utf-8", "replace").strip()
        if not s.startswith("data:"):
            continue
        payload = s[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        if ev.get("type") == "text_delta":
            text_parts.append(ev.get("text", ""))
        elif ev.get("type") == "done":
            break
    conn.close()
    return "".join(text_parts)


def run_demo_ui(base, email, pw, sentinel, shots):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))

        page.goto(base, wait_until="networkidle")
        ins = page.locator("input:visible")
        ins.nth(0).fill(email)
        ins.nth(1).fill(pw)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3200)
        print("  UI 登录完成")

        # 打开刚才那个会话
        page.locator(".session-item").first.click()
        page.wait_for_timeout(1500)
        page.screenshot(path=str(shots / "d01_session.png"))

        # 搜索哨兵
        page.keyboard.press("Control+Shift+K")
        page.wait_for_timeout(600)
        page.fill(".ss-input-row input", sentinel)
        page.wait_for_timeout(1600)
        rows = page.locator(".ss-row").count()
        marks = page.locator(".ss-mark").count()
        print(f"  UI 搜索 -> {rows} 行, {marks} 处高亮")
        page.screenshot(path=str(shots / "d02_search.png"))

        jumped = False
        if rows:
            page.locator(".ss-row").first.click()
            page.wait_for_timeout(2200)
            jumped = page.locator(".ss-panel").count() == 0
            print(f"  跳转后面板关闭: {jumped}")

        state = page.evaluate("""() => ({
            app: !!document.querySelector('.app'),
            text: (document.body.innerText || '').trim().length,
            msgs: document.querySelectorAll('[data-msg-id]').length,
        })""")
        print(f"  最终状态: {state}")
        page.screenshot(path=str(shots / "d03_after.png"))

        alive = state["app"] and state["text"] > 20
        print(f"  无白屏: {alive}, 页面异常: {errs[:2] if errs else '无'}")
        browser.close()
        return bool(alive and rows and jumped and not errs)


if __name__ == "__main__":
    raise SystemExit(main())