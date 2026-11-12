"""端到端验证：设置页「刷新模型列表」是否真的拉到端点真实模型。

覆盖用户报的场景：hx 在湛卢下选了预制短名 qwen3-max → 403。
改后应当：点刷新 → 出现 moma/qwen3.7-max 等真实 ID → 选中保存 → 对话可正常出字。
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

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work" / "discover_db"
SHOT = ROOT / "work" / "discover_shots"
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
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode() or "{}")


def main():
    # 需要真实湛卢 key
    key = ""
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ZhanLu_API_KEY="):
            key = line.split("=", 1)[1].strip()
    if not key:
        print("没有 ZhanLu_API_KEY，跳过真实链路")
        return 1

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

        sfx = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        email = f"disc_{sfx}@example.invalid"
        pw = "Disc#12345"
        tok = call(base, "/api/auth/register",
                   {"email": email, "password": pw, "name": "模型发现测试"})["access_token"]
        print(f"账号 {email}")

        print("\n=== A. 真实湛卢端点探测（HTTP 层）===")
        r = call(base, "/api/me/llm-models-probe",
                 {"base_url": "https://ecloud.10086.cn/api/query/aigateway/v1",
                  "api_key": key}, token=tok)
        print(f"  ok={r['ok']}  模型数={len(r['models'])}  endpoint={r['endpoint']}")
        ids = [m["id"] for m in r["models"]]
        print(f"  样例: {ids[:4]}")
        has_qwen = any("qwen" in i for i in ids)
        has_short = "qwen3-max" in ids
        print(f"  含带前缀的 qwen: {has_qwen}")
        print(f"  含前端曾硬编码的短名 'qwen3-max': {has_short}  (应为 False)")

        print("\n=== B. 坏 key 的错误提示 ===")
        r2 = call(base, "/api/me/llm-models-probe",
                  {"base_url": "https://ecloud.10086.cn/api/query/aigateway/v1",
                   "api_key": "sk-obviously-bad"}, token=tok)
        print(f"  ok={r2['ok']}  error={r2['error']!r}")
        print(f"  hint={r2['hint'][:60]!r}")
        print(f"  (前端会把这个 hint 显示在模型名下方)")

        print("\n=== C. 浏览器：设置页刷新模型列表 ===")
        ok_ui = run_ui(base, email, pw, SHOT, ids)
        return 0 if (r["ok"] and has_qwen and not has_short and ok_ui) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def run_ui(base, email, pw, shots, expected_ids):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 980})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))

        page.goto(base, wait_until="networkidle")
        ins = page.locator("input:visible")
        ins.nth(0).fill(email)
        ins.nth(1).fill(pw)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)

        # 打开设置
        page.locator(".user-chip").click()
        page.wait_for_timeout(900)
        page.screenshot(path=str(shots / "s1_settings.png"))

        # 填 base_url + key（湛卢）
        base_input = page.locator("input").filter(has=page.locator("xpath=.")).nth(0)
        # 直接按 label 文本定位更稳
        page.locator("label", has_text="Base URL").locator("input").fill(
            "https://ecloud.10086.cn/api/query/aigateway/v1")
        page.locator("label", has_text="API Key").locator("input").fill(
            load_key())
        page.wait_for_timeout(300)

        # 点刷新模型列表
        btn = page.locator("button", has_text="刷新模型列表")
        before = btn.count()
        print(f"  刷新按钮存在: {before > 0}")
        btn.first.click()
        page.wait_for_timeout(8000)

        state = page.evaluate("""() => {
            const sel = [...document.querySelectorAll('label')]
                .find(l => l.textContent.includes('模型名'))
                ?.querySelector('select');
            const opts = sel ? [...sel.options].map(o => o.value) : [];
            const err = [...document.querySelectorAll('.field-error')]
                .map(e => e.textContent);
            const okHint = [...document.querySelectorAll('.field-help.ok')]
                .map(e => e.textContent);
            return {opts, n: opts.length, err, okHint,
                    app: !!document.querySelector('.app')};
        }""")
        print(f"  下拉选项数: {state['n']}")
        print(f"  前 5 项: {state['opts'][:5]}")
        print(f"  成功提示: {state['okHint']}")
        print(f"  错误提示: {state['err']}")
        page.screenshot(path=str(shots / "s2_models_loaded.png"))

        real = set(state["opts"])
        got_qwen = any("qwen3.7-max" in o for o in real)
        no_short = "qwen3-max" not in real

        # 选一个真实模型并保存
        saved = False
        target = next((o for o in state["opts"] if "qwen3.7-max" in o), None)
        if target:
            page.locator("label", has_text="模型名").locator("select").select_option(target)
            page.locator("label", has_text="标签").locator("input").fill("湛卢qwen")
            # 按钮文案是「新增配置 / 保存修改」，不是「保存」——按类名更稳
            page.locator(".form-actions .new-btn").first.click()
            page.wait_for_timeout(2500)
            saved = page.locator(".cfg-item", has_text="湛卢qwen").count() > 0
            print(f"  保存成功（配置列表出现该项）: {saved}")
            page.screenshot(path=str(shots / "s3_saved.png"))

        print(f"  应用存活: {state['app']}, 页面异常: {errs[:2] if errs else '无'}")
        browser.close()

        ok = state["n"] > 0 and got_qwen and no_short and saved and not errs
        print(f"\n  {'PASS' if ok else 'FAIL'} 下拉来自真实端点、无遗留短名、可保存")
        return ok


def load_key():
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ZhanLu_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


if __name__ == "__main__":
    raise SystemExit(main())