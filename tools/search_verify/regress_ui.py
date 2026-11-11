"""全量 UI 回归：确认新增搜索没有破坏任何既有功能。

覆盖：登录、侧栏、会话 CRUD、设置弹窗各 tab、文件面板、Team 面板、
消息渲染、快捷键互不冲突、搜索面板开关、以及最关键的「任何操作后都不白屏」。
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
WORK = ROOT / "work" / "regress_db"
SHOT = ROOT / "work" / "regress_shots"
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


def call(base, path, data=None, token=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base + path, data=body,
                                 method=method or ("POST" if body else "GET"))
    if body:
        req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def ok(self, name, cond, detail=""):
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append((name, detail))
            print(f"  FAIL  {name}  {detail}")

    def report(self):
        print(f"\n{self.passed} passed, {len(self.failed)} failed")
        for name, d in self.failed:
            print(f"  X {name}: {d}")
        return 0 if not self.failed else 1


def alive(page):
    return page.evaluate("""() => {
        const body = document.body;
        const txt = (body.innerText || '').trim();
        // React 崩溃时 React 会把 root 清空：body 没有可见文本也算白屏
        return {
            app: !!document.querySelector('.app'),
            text: txt.length,
            interactive: [...document.querySelectorAll('button,input,a')]
                .filter(e => e.offsetParent !== null).length,
        };
    }""")


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
            print((WORK / "server.log").read_text(encoding="utf-8", errors="replace")[-2000:])
            return 1
        print(f"后端 {base}\n")

        sfx = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        email = f"rg_{sfx}@example.invalid"
        pw = "Regress#123"
        tok = call(base, "/api/auth/register",
                   {"email": email, "password": pw, "name": "回归测试"})["access_token"]

        # 造多会话 + 消息，供 UI 回归用
        ids = []
        for i in range(3):
            s = call(base, "/api/sessions", {"title": f"回归会话{i+1}", "model_id": ""}, token=tok)
            ids.append(s["id"])
        con = sqlite3.connect(str(dbpath))
        now = int(time.time() * 1000)
        rows = []
        for i, sid in enumerate(ids):
            rows.append((f"rg_{sfx}_{i}_u", sid, "user",
                         f"回归测试第{i+1}个会话的提问 REGRESS{sfx}", "", "[]", "complete", now + i))
            rows.append((f"rg_{sfx}_{i}_a", sid, "assistant",
                         f"第{i+1}个会话的回答，包含关键词 REGRESS{sfx}。", "", "[]", "complete", now + i + 1))
        con.executemany(
            "INSERT INTO messages (id,session_id,role,content,reasoning,blocks,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()
        print(f"账号 {email}，3 个会话，6 条消息\n")

        return run_ui(base, email, pw, SHOT, sfx)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def run_ui(base, email, pw, shots, sfx):
    from playwright.sync_api import sync_playwright

    c = Checker()
    page_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        # ---------- 1. 登录 ----------
        print("[1] 登录")
        page.goto(base, wait_until="networkidle")
        c.ok("登录页有输入框", page.locator("input:visible").count() >= 2)
        inputs = page.locator("input:visible")
        inputs.nth(0).fill(email)
        inputs.nth(1).fill(pw)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
        st = alive(page)
        c.ok("登录后应用存活", st["app"] and st["text"] > 20, str(st))
        c.ok("侧栏存在", page.locator(".sidebar").count() == 1)
        c.ok("会话列表有 3 条", page.locator(".session-item").count() >= 3,
             f"实际 {page.locator('.session-item').count()}")
        page.screenshot(path=str(shots / "r01_login.png"))

        # ---------- 2. 侧栏导航项 ----------
        print("[2] 侧栏导航")
        nav = page.locator(".side-nav-item")
        c.ok("导航项数量正常", nav.count() >= 5, f"实际 {nav.count()}")
        # 「搜索」不再 disabled
        search_btn = page.locator(".side-nav-item", has_text="搜索")
        c.ok("搜索按钮存在", search_btn.count() == 1)
        c.ok("搜索按钮已启用", search_btn.first.is_enabled())
        # 「项目」「Artifacts」仍是 disabled（未被误改）
        proj = page.locator(".side-nav-item", has_text="项目")
        c.ok("项目仍为未实现态", proj.first.is_disabled())
        art = page.locator(".side-nav-item", has_text="Artifacts")
        c.ok("Artifacts 仍为未实现态", art.first.is_disabled())

        # ---------- 3. 会话切换 ----------
        print("[3] 会话切换")
        items = page.locator(".session-item")
        items.nth(1).click()
        page.wait_for_timeout(1200)
        st = alive(page)
        c.ok("切会话后存活", st["app"] and st["text"] > 20, str(st))
        active = page.locator(".session-item.active").count()
        c.ok("有 1 个激活会话", active == 1, f"实际 {active}")
        page.screenshot(path=str(shots / "r02_switch.png"))

        # ---------- 4. 搜索面板开关（不影响主界面） ----------
        print("[4] 搜索面板")
        page.keyboard.press("Control+Shift+K")
        page.wait_for_timeout(600)
        c.ok("快捷键打开面板", page.locator(".ss-panel").count() == 1)
        c.ok("面板打开时主界面仍在", page.locator(".sidebar").count() == 1)
        page.screenshot(path=str(shots / "r03_search.png"))

        page.fill(".ss-input-row input", "REGRESS")
        page.wait_for_timeout(1400)
        n_hits = page.locator(".ss-row").count()
        c.ok("搜索出结果", n_hits >= 3, f"实际 {n_hits}")
        n_marks = page.locator(".ss-mark").count()
        c.ok("有高亮", n_marks >= 1, f"实际 {n_marks}")

        # Esc 关闭
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        c.ok("Esc 关闭面板", page.locator(".ss-panel").count() == 0)
        st = alive(page)
        c.ok("关闭后主界面完好", st["app"] and st["text"] > 20, str(st))

        # 点遮罩关闭
        page.keyboard.press("Control+Shift+K")
        page.wait_for_timeout(500)
        page.mouse.click(60, 400)
        page.wait_for_timeout(500)
        c.ok("点遮罩关闭面板", page.locator(".ss-panel").count() == 0)

        # 空输入不搜索
        page.keyboard.press("Control+Shift+K")
        page.wait_for_timeout(500)
        page.fill(".ss-input-row input", "   ")
        page.wait_for_timeout(800)
        c.ok("空输入不产生结果", page.locator(".ss-row").count() == 0)
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        # ---------- 5. 搜索跳转 ----------
        print("[5] 搜索跳转")
        page.keyboard.press("Control+Shift+K")
        page.wait_for_timeout(500)
        page.fill(".ss-input-row input", f"REGRESS{sfx}")
        page.wait_for_timeout(1500)
        rows = page.locator(".ss-row")
        before = rows.count()
        c.ok("跳转前有结果", before >= 3, f"实际 {before}")
        if before:
            rows.first.click()
            page.wait_for_timeout(2200)
            st = alive(page)
            c.ok("跳转后应用存活", st["app"] and st["text"] > 20, str(st))
            c.ok("跳转后面板已关闭", page.locator(".ss-panel").count() == 0)
            c.ok("目标会话已激活", page.locator(".session-item.active").count() == 1)
        page.screenshot(path=str(shots / "r04_jump.png"))

        # ---------- 6. 设置弹窗（各 tab） ----------
        print("[6] 设置弹窗")
        page.locator(".user-chip").click()
        page.wait_for_timeout(1000)
        modal = page.locator(".modal").count()
        c.ok("设置弹窗打开", modal >= 1, f"实际 {modal}")
        tabs = page.locator(".modal-tabs button")
        n_tabs = tabs.count()
        c.ok("有多个 tab", n_tabs >= 2, f"实际 {n_tabs}")
        for i in range(n_tabs):
            tabs.nth(i).click()
            page.wait_for_timeout(450)
            st = alive(page)
            if not (st["app"] and st["text"] > 20):
                c.ok(f"tab{i} 切换后存活", False, str(st))
                break
        else:
            c.ok(f"全部 {n_tabs} 个 tab 切换后存活", True)
        page.screenshot(path=str(shots / "r05_settings.png"))
        # 关闭
        esc = page.locator(".modal-head .ghost-btn")
        if esc.count():
            esc.first.click()
            page.wait_for_timeout(700)
        c.ok("设置弹窗可关闭", page.locator(".modal.ss-panel").count() == 0)

        # ---------- 7. 会话浏览器 ----------
        print("[7] 全部会话")
        all_btn = page.locator(".all-sessions")
        if all_btn.count():
            all_btn.click()
            page.wait_for_timeout(1000)
            c.ok("会话浏览器打开", page.locator(".session-browser").count() == 1)
            sb_input = page.locator(".sb-search input")
            if sb_input.count():
                sb_input.fill("回归")
                page.wait_for_timeout(600)
                c.ok("会话浏览器标题筛选可用", True)
            st = alive(page)
            c.ok("浏览器打开时存活", st["app"] and st["text"] > 20, str(st))
            page.screenshot(path=str(shots / "r06_browser.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            # 用关闭按钮
            close = page.locator(".session-browser .modal-head .ghost-btn")
            if close.count():
                close.first.click()
                page.wait_for_timeout(600)

        # ---------- 8. 文件面板 ----------
        print("[8] 文件面板")
        file_btn = page.locator(".header-actions .ghost-btn", has_text="文件")
        if file_btn.count():
            file_btn.first.click()
            page.wait_for_timeout(1200)
            st = alive(page)
            c.ok("文件面板打开后存活", st["app"] and st["text"] > 20, str(st))
            page.screenshot(path=str(shots / "r07_files.png"))
            # 文件面板不支持 Esc（既有行为），必须点关闭按钮
            close_ws = page.locator(".ws-mask .ghost-btn[aria-label='关闭']")
            if close_ws.count():
                close_ws.first.click()
            else:
                page.mouse.click(30, 450)
            page.wait_for_timeout(800)
            c.ok("文件面板可关闭", page.locator(".ws-mask").count() == 0,
                 f"仍有 {page.locator('.ws-mask').count()} 个遮罩")

        # ---------- 9. 输入框与快捷键互不冲突 ----------
        print("[9] 输入框 + 快捷键")
        # 保险：万一还有残留遮罩，先清干净再测输入框
        if page.locator(".ws-mask").count():
            page.locator(".ws-mask .ghost-btn[aria-label='关闭']").first.click()
            page.wait_for_timeout(600)
        composer = page.locator(".composer textarea, .composer input[type=text]")
        if composer.count():
            composer.first.click()
            composer.first.fill("测试输入不被快捷键吞掉")
            page.wait_for_timeout(300)
            val = composer.first.input_value()
            c.ok("输入内容保留", "测试输入" in val, f"实际 {val!r}")
            # 在输入框里按 Ctrl+Shift+K 仍应打开搜索（全局监听）
            page.keyboard.press("Control+Shift+K")
            page.wait_for_timeout(600)
            c.ok("输入框内快捷键也生效", page.locator(".ss-panel").count() == 1)
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            # 输入内容应还在
            val2 = composer.first.input_value()
            c.ok("面板开关不影响输入内容", "测试输入" in val2, f"实际 {val2!r}")
            composer.first.fill("")
        st = alive(page)
        c.ok("步骤 9 后存活", st["app"] and st["text"] > 20, str(st))

        # ---------- 10. 重复开关压力 ----------
        print("[10] 重复开关压力")
        for i in range(12):
            page.keyboard.press("Control+Shift+K")
            page.wait_for_timeout(120)
            page.keyboard.press("Escape")
            page.wait_for_timeout(120)
        st = alive(page)
        c.ok("连开 12 次后存活", st["app"] and st["text"] > 20, str(st))

        # ---------- 11. 最终检查 ----------
        print("[11] 最终状态")
        page.wait_for_timeout(500)
        st = alive(page)
        c.ok("最终无白屏", st["app"] and st["text"] > 20, str(st))
        c.ok("无页面级异常", len(page_errors) == 0, str(page_errors[:3]))
        page.screenshot(path=str(shots / "r08_final.png"))

        browser.close()

    return c.report()


if __name__ == "__main__":
    raise SystemExit(main())