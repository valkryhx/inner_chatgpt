"""真实用户路径验证：填 URL → 填 key → 失焦 → 点下拉，全程不点刷新按钮。"""
from __future__ import annotations

import os
import random
import shutil
import string
import subprocess
import sys

sys.path.insert(0, "tools")
from verify_model_discovery import BOOT, PY, ROOT, SHOT, WORK, call, free_port, load_key, wait_health  # noqa

key = load_key()
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

SIG = """() => {
    const s = [...document.querySelectorAll('label')]
      .find(l => l.textContent.includes('模型名'))?.querySelector('select');
    return s ? [...s.options].map(o => o.value).filter(v => v !== '__custom__') : [];
}"""

try:
    assert wait_health(base), "后端启动失败"
    sfx = "".join(random.choices(string.ascii_lowercase, k=6))
    email = f"fin_{sfx}@example.invalid"
    call(base, "/api/auth/register", {"email": email, "password": "Disc#12345", "name": "final"})

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page(viewport={"width": 1440, "height": 980})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(base, wait_until="networkidle")
        ins = page.locator("input:visible")
        ins.nth(0).fill(email)
        ins.nth(1).fill("Disc#12345")
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
        page.locator(".user-chip").click()
        page.wait_for_timeout(900)

        # 真实路径：先填 URL，再填 key，然后点下拉（这会让 key 失焦）
        page.locator("label", has_text="Base URL").locator("input").first.fill(
            "https://ecloud.10086.cn/api/query/aigateway/v1")
        page.locator("label", has_text="API Key").locator("input").first.fill(key)

        sel = page.locator("label", has_text="模型名").locator("select")
        sel.click()          # 鼠标挪到下拉 = key 失焦 = 探测启动
        print("点开下拉，等待列表填充…")

        opts = []
        for i in range(20):
            page.wait_for_timeout(1000)
            opts = page.evaluate(SIG)
            if len(opts) > 3:
                print(f"  {i+1}s: {len(opts)} 个模型")
                break
        print("前 5 项:", opts[:5])

        hit = any("qwen3.7-max" in o for o in opts)
        no_short = "qwen3-max" not in opts
        print(f"含真实 ID moma/qwen3.7-max: {hit}")
        print(f"含旧硬编码短名 qwen3-max: {not no_short}  (应 False)")
        page.screenshot(path=str(SHOT / "s5_dropdown_real_path.png"), full_page=True)

        # 选中并保存，验证能落库
        saved = False
        target = next((o for o in opts if o == "moma/qwen3.7-max"), None)
        if target:
            sel.select_option(target)
            page.locator("label", has_text="标签").locator("input").first.fill("湛卢qwen")
            page.locator(".form-actions .new-btn").first.click()
            page.wait_for_timeout(2500)
            saved = page.locator(".cfg-item", has_text="湛卢qwen").count() > 0
            print(f"保存成功: {saved}")
            page.screenshot(path=str(SHOT / "s6_saved.png"), full_page=True)

        print(f"页面异常: {errs[:2] if errs else '无'}")
        b.close()

    ok = len(opts) > 3 and hit and no_short and saved and not errs
    print(("\nPASS" if ok else "\nFAIL") + " 纯下拉（不点刷新按钮）拿到端点真实模型并可保存")
    raise SystemExit(0 if ok else 1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
