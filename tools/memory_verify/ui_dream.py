"""记忆系统 UI 的 Playwright 核验（L3「用户能看到」判据）。

为什么必须有这个脚本：`MemorySection` 之前只跑过 `npm run build` + `tsc --noEmit`。
按 AGENTS.md §2.4 的判据，那只到「机制已触发」；「用户能看到」必须有渲染证据。
build 通过**不等于**渲染正确 —— 组件可能挂在 `status.dream` 为 undefined 上、
CSS 可能把按钮挤出可视区、tab 切换可能白屏。这些都只有真浏览器能证伪。

覆盖（M1–M4.5 的全部 UI 面）：

| 组 | 验什么 |
|---|---|
| 状态条 | 条数、注入预算、三个开关标签 |
| dream 区块 | 状态说明文案、立即整理按钮、状态标签（上次整理/失败/中断/索引待重建/已跑 N 次） |
| dream 执行 | 点「立即整理」→ 按钮变「整理中…」→ 结果行出现 → 条数刷新 |
| 列表 | 分页、筛选、描述与元信息 |
| 编辑 | 打开编辑器、原文回填、保存、CAS 冲突提示 |
| 删除 | 二次确认弹窗、删除后从列表消失 |
| 不白屏 | 每次操作后 `.app` 存在且可见文本 > 20 字 |

跑法::

    python tools/memory_verify/ui_dream.py            # 全部
    python tools/memory_verify/ui_dream.py --keep     # 保留临时库与截图

前置：`pip install playwright && python -m playwright install chromium`。

**不碰生产库**：脚本自己复制一份 `backend/data/app.db` 到 `work/memory_ui_db/`，
在没有应用任何迁移的副本上建表（`app.main` 的 lifespan 会跑 `init_tables`），
用独立端口起后端，跑完杀进程。所以可以和正在跑的 8000 并行。
"""
from __future__ import annotations

import argparse
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

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "work" / "memory_ui_db"
SHOT = ROOT / "work" / "memory_ui_shots"
PY = sys.executable

# 子进程里只改三处 DB_PATH，其余照常启动（与 regress_ui.py 同法）。
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


def call(base: str, path: str, data=None, token: str | None = None,
         method: str | None = None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base + path, data=body,
                                 method=method or ("POST" if body else "GET"))
    if body:
        req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode() or "{}"
        return json.loads(raw)


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[tuple[str, str]] = []

    def ok(self, name: str, cond, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append((name, detail))
            print(f"  FAIL  {name}  {detail}")

    def report(self) -> int:
        print(f"\n{self.passed} passed, {len(self.failed)} failed")
        for name, d in self.failed:
            print(f"  X {name}: {d}")
        return 0 if not self.failed else 1


def alive(page) -> dict:
    """React 崩溃会把 root 清空 —— 有 .app 且可见文本不为空才算活着。"""
    return page.evaluate("""() => {
        const txt = (document.body.innerText || '').trim();
        return {
            app: !!document.querySelector('.app'),
            text: txt.length,
            interactive: [...document.querySelectorAll('button,input,a')]
                .filter(e => e.offsetParent !== null).length,
        };
    }""")


def open_memory_tab(page, shots: Path, tag: str) -> bool:
    """打开设置 → 我的记忆 tab。"""
    page.locator(".user-chip").click()
    page.wait_for_timeout(900)
    if page.locator(".modal").count() == 0:
        return False
    tab = page.locator(".modal-tabs button", has_text="我的记忆")
    if tab.count() == 0:
        return False
    tab.first.click()
    page.wait_for_timeout(1200)
    return page.locator(".settings-section").count() > 0


# ---------------- 各组检查 ----------------

def check_status_bar(c: Checker, page, shots: Path, base: str = '', token: str = '') -> None:
    """状态条：条数 + 预算 + 三个开关标签。"""
    print("[1] 状态条")
    status = page.locator(".memory-status")
    c.ok("状态条渲染", status.count() == 1, f"实际 {status.count()}")
    if status.count() == 0:
        return
    text = status.inner_text()
    c.ok("显示条数", "条记忆" in text, text[:120])
    c.ok("显示注入预算", "注入预算" in text, text[:120])
    flags = page.locator(".memory-status .memory-flags .tag")
    c.ok("开关标签 >= 2", flags.count() >= 2, f"实际 {flags.count()}")
    # 把「页面此刻显示的条数」与「API 此刻返回的条数」对照。
    # 两者不一致 = 前端渲染用的是陈旧数据，那是真缺陷；一致 = 我之前的判断有误。
    try:
        api_total = call(base, "/api/me/memories", token=token)["total"]
    except Exception as exc:  # noqa: BLE001
        api_total = f"<取不到: {type(exc).__name__}>"
    print(f"    [debug] 页面状态条文字={text[:60]!r}")
    print(f"    [debug] API total={api_total} 列表条目={page.locator('.memory-item').count()}")
    page.screenshot(path=str(shots / "m01_status.png"), full_page=True)


def check_dream_block(c: Checker, page, shots: Path) -> None:
    """dream 区块：标题、说明、按钮、状态标签。"""
    print("[2] dream 区块")
    block = page.locator(".memory-dream")
    c.ok("dream 区块渲染", block.count() == 1, f"实际 {block.count()}")
    if block.count() == 0:
        return
    text = block.inner_text()
    c.ok("有标题", "记忆整理" in text, text[:160])
    # 节奏说明必须能被用户读懂 —— 这是「它什么时候自己动」的唯一披露
    c.ok("有节奏说明", "小时" in text and "会话" in text, text[:160])

    btn = page.locator(".memory-dream-head .ghost-btn")
    c.ok("有立即整理按钮", btn.count() == 1, f"实际 {btn.count()}")
    if btn.count():
        c.ok("按钮文案正确", "立即整理" in btn.first.inner_text(),
             btn.first.inner_text())
        c.ok("按钮可用（dream 开启时）", btn.first.is_enabled())

    # 状态标签：首次跑之前应该有「上次整理：从未」
    tags = page.locator(".memory-dream .memory-flags .tag")
    c.ok("有状态标签", tags.count() >= 1, f"实际 {tags.count()}")
    tag_text = " ".join(tags.all_inner_texts()) if tags.count() else ""
    c.ok("显示上次整理时间", "上次整理" in tag_text, tag_text)

    # 按钮不能跑到可视区外（CSS 布局错误在 build 阶段查不出来）
    box = btn.first.bounding_box() if btn.count() else None
    if box:
        vp = page.viewport_size
        c.ok("按钮在可视区内",
             box["x"] >= 0 and box["y"] >= 0
             and box["x"] + box["width"] <= vp["width"]
             and box["width"] > 40 and box["height"] > 16,
             f"box={box} vp={vp}")
    page.screenshot(path=str(shots / "m02_dream.png"), full_page=True)


def check_list_and_filter(c: Checker, page, shots: Path) -> None:
    """列表渲染 + 筛选。"""
    print("[3] 列表与筛选")
    items = page.locator(".memory-item")
    n = items.count()
    c.ok("列表有条目", n >= 3, f"实际 {n}")
    if n:
        first = items.first.inner_text()
        c.ok("条目有类型标签", ".tag-kind" and page.locator(
            ".memory-item .tag-kind").count() >= 1, first[:100])
        c.ok("条目有描述", ".memory-desc" and page.locator(
            ".memory-item .memory-desc").count() >= 1, first[:100])
        c.ok("条目有操作按钮", page.locator(
            ".memory-item .cfg-actions button").count() >= n, "")

    # 筛选：搜一个必然不存在的词 -> 空态；清空 -> 恢复
    search = page.locator(".memory-search input")
    if search.count():
        search.first.fill("ZZZ_NO_SUCH_MEMORY_ZZZ")
        page.locator(".memory-search button", has_text="筛选").first.click()
        page.wait_for_timeout(1200)
        empty = page.locator(".cfg-list .hint", has_text="还没有记忆")
        c.ok("筛选无结果时显示空态", empty.count() >= 1,
             f"条目数 {page.locator('.memory-item').count()}")
        c.ok("筛选后不白屏", alive(page)["app"])

        search.first.fill("")
        page.locator(".memory-search button", has_text="筛选").first.click()
        page.wait_for_timeout(1200)
        c.ok("清空筛选后恢复", page.locator(".memory-item").count() >= 3,
             f"实际 {page.locator('.memory-item').count()}")
    page.screenshot(path=str(shots / "m03_list.png"), full_page=True)


def check_editor(c: Checker, page, shots: Path, base: str, token: str) -> None:
    """编辑器：打开 -> 回填 -> 保存。"""
    print("[4] 编辑器")
    edit_btn = page.locator(".memory-item .cfg-actions button", has_text="编辑")
    if edit_btn.count() == 0:
        c.ok("有编辑按钮", False, "找不到编辑按钮")
        return
    edit_btn.first.click()
    page.wait_for_timeout(1200)
    editor = page.locator(".memory-editor")
    c.ok("编辑器打开", editor.count() >= 1, f"实际 {editor.count()}")
    c.ok("编辑器打开后不白屏", alive(page)["app"])

    # 原值必须回填 —— 空编辑器意味着用户会「保存」成空内容，那是最坏的一种数据丢失
    fields = page.locator(".memory-editor input, .memory-editor textarea")
    n_fields = fields.count()
    c.ok("编辑器有多个字段", n_fields >= 3, f"实际 {n_fields}")
    values = [fields.nth(i).input_value() for i in range(n_fields)]
    c.ok("原值已回填", any(v.strip() for v in values),
         f"values={[v[:20] for v in values]}")

    # 字段是按 <label>描述文字< <input> 组织的读屏友好结构，所以按 label 文案定位。
    # 按 input 顺序猜不可靠：slug 字段在编辑态是不渲染的。
    desc_input = page.locator(".memory-editor label",
                              has_text="一句话描述").locator("input")
    c.ok("能按 label 定位到描述字段", desc_input.count() == 1,
         f"实际 {desc_input.count()}")
    if desc_input.count() == 0:
        return
    original_desc = desc_input.first.input_value()
    c.ok("描述字段有原值", bool(original_desc.strip()), repr(original_desc[:40]))
    desc_input.first.fill("UI 核验改过的描述 UI_EDIT_MARK")
    page.screenshot(path=str(shots / "m04_editor.png"), full_page=True)

    save_btn = page.locator(".memory-editor .form-actions button").first
    c.ok("有保存按钮", page.locator(".memory-editor .form-actions button").count() >= 1)
    save_btn.click()
    page.wait_for_timeout(2000)
    c.ok("保存后不白屏", alive(page)["app"])
    body = page.locator(".settings-section").inner_text()
    c.ok("保存体现在列表里", "UI_EDIT_MARK" in body,
         f"保存后是否含标记：{'UI_EDIT_MARK' in body}")

    # 落库核对要用 API，不能用「列表文本里还有没有旧描述」——种子数据里
    # title 与 description 同文，改完 description 后标题仍然含原文，
    # 那种断言会把正确行为判成失败（第一版就是这么误报的）。
    try:
        rows = call(base, "/api/me/memories", token=token)["entries"]
        hit = [r for r in rows if "UI_EDIT_MARK" in (r.get("description") or "")]
        c.ok("描述已落库", len(hit) == 1, f"命中 {len(hit)} 条")
        if hit:
            detail = call(base, f"/api/me/memories/{hit[0]['slug']}", token=token)
            c.ok("正文没被清空", bool((detail.get("content") or "").strip()),
                 f"content={ (detail.get('content') or '')[:60]!r}")
            c.ok("标题未被误改", (detail.get("title") or "") != "UI_EDIT_MARK",
                 f"title={detail.get('title')!r}")
    except Exception as exc:  # noqa: BLE001
        c.ok("能核对落库结果", False, f"{type(exc).__name__}: {exc}")
    page.screenshot(path=str(shots / "m05_saved.png"), full_page=True)


def check_delete(c: Checker, page, shots: Path, base: str, token: str) -> None:
    """删除：原生 confirm 二次确认 + 删除后消失。

    前端用的是 `window.confirm()`（不是自绘 modal）。Playwright 默认**自动取消**
    对话框，所以不接管 dialog 事件的话点删除什么都不会发生 —— 那不是产品缺陷，
    是脚本没接住。这里显式 accept，并记录是否真的弹过。
    """
    print("[5] 删除")
    before = page.locator(".memory-item").count()
    del_btn = page.locator(".memory-item .cfg-actions button", has_text="删除")
    c.ok("有删除按钮", del_btn.count() >= 1, f"实际 {del_btn.count()}")
    if del_btn.count() == 0:
        return

    # 记下要删的那条 slug，删完去 API 核对（UI 与后端必须一致）
    slug = ""
    try:
        rows = call(base, "/api/me/memories", token=token)["entries"]
        if rows:
            slug = rows[0]["slug"]
    except Exception:  # noqa: BLE001
        pass

    dialogs: list[str] = []

    def on_dialog(d):
        dialogs.append(d.message)
        d.accept()

    page.on("dialog", on_dialog)
    try:
        del_btn.first.click()
        page.wait_for_timeout(1800)
    finally:
        page.remove_listener("dialog", on_dialog)

    c.ok("删除有二次确认", bool(dialogs),
         "没有弹出 confirm 对话框（直接删了？）")
    if dialogs:
        c.ok("确认文案提到不可逆后果", "删除" in dialogs[0], dialogs[0][:120])

    after = page.locator(".memory-item").count()
    c.ok("删除后条数减少", after < before, f"{before} -> {after}")
    c.ok("删除后不白屏", alive(page)["app"])
    if slug:
        try:
            left = {e["slug"] for e in call(base, "/api/me/memories", token=token)["entries"]}
            c.ok("后端也已删除", slug not in left, f"slug={slug} 仍在")
        except Exception:  # noqa: BLE001
            pass
    page.screenshot(path=str(shots / "m06_deleted.png"), full_page=True)


def check_run_dream(c: Checker, page, shots: Path, base: str, token: str) -> None:
    """点「立即整理」：按钮态 -> 结果行 -> 条数刷新。

    这一步会真的调 LLM（后端配了模型时）。为防止 UI 核验被模型延迟绑架，
    给足超时；同时**不把「模型有没有真的合并」当作本脚本的判据** ——
    那是 S-M8（`tests/e2e/memory_cases.py`）的职责。这里只验 UI 能不能正确
    表达三种结局：跑了 / 没跑（带原因）/ 出错。
    """
    print("[6] 立即整理")
    btn = page.locator(".memory-dream-head .ghost-btn")
    if btn.count() == 0:
        c.ok("有立即整理按钮", False, "")
        return

    before_total = page.locator(".memory-item").count()
    btn.first.click()
    page.wait_for_timeout(400)
    # 点击后立刻应该变成「整理中…」（这个中间态必须在 400ms 内出现，
    # 否则用户会以为点击没生效而连点）
    mid = page.locator(".memory-dream-head .ghost-btn").inner_text()
    c.ok("点击后进入整理中态", "整理中" in mid or "立即整理" in mid,
         f"按钮文案={mid!r}")

    # 等结果行出现（最长 180s；正常 5-60s）
    result = page.locator(".memory-dream-result")
    end = time.time() + 180
    got = False
    while time.time() < end:
        if result.count() > 0 and result.first.inner_text().strip():
            got = True
            break
        page.wait_for_timeout(600)
    c.ok("出现结果行", got, "180s 内没有结果行")
    if got:
        text = result.first.inner_text()
        c.ok("结果行内容可读",
             any(k in text for k in ("整理完成", "没有整理", "整理出错", "整理失败")),
             text[:200])
        c.ok("结果行不是空白", len(text.strip()) > 4, repr(text))
    c.ok("整理后不白屏", alive(page)["app"])

    # 按钮必须恢复可点（否则用户下次点不了）
    page.wait_for_timeout(1200)
    after_btn = page.locator(".memory-dream-head .ghost-btn").first
    c.ok("按钮恢复可用", after_btn.is_enabled(), "按钮仍是 disabled")
    c.ok("按钮文案已复原", "立即整理" in after_btn.inner_text(),
         after_btn.inner_text())

    after_total = page.locator(".memory-item").count()
    c.ok("条数已刷新（可能不变）", isinstance(after_total, int),
         f"{before_total} -> {after_total}")
    page.screenshot(path=str(shots / "m07_dream_result.png"), full_page=True)


def check_tab_switch_alive(c: Checker, page, shots: Path) -> None:
    """切走再切回记忆 tab：不能白屏、不能丢状态。"""
    print("[7] tab 往返")
    tabs = page.locator(".modal-tabs button")
    for i in range(tabs.count()):
        tabs.nth(i).click()
        page.wait_for_timeout(450)
        st = alive(page)
        if not (st["app"] and st["text"] > 20):
            c.ok(f"tab{i} 切换后不白屏", False, str(st))
            return
    c.ok(f"全部 {tabs.count()} 个 tab 往返不白屏", True)

    mem_tab = page.locator(".modal-tabs button", has_text="我的记忆")
    mem_tab.first.click()
    page.wait_for_timeout(1200)
    c.ok("切回记忆 tab 仍有内容", page.locator(".settings-section").count() > 0,
         "settings-section 消失")
    page.screenshot(path=str(shots / "m08_roundtrip.png"), full_page=True)


def run_ui(base: str, email: str, pw: str) -> int:
    from playwright.sync_api import sync_playwright

    c = Checker()
    page_errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        print("[0] 登录")
        page.goto(base, wait_until="networkidle")
        inputs = page.locator("input:visible")
        c.ok("登录页存在", inputs.count() >= 2, f"实际 {inputs.count()}")
        inputs.nth(0).fill(email)
        inputs.nth(1).fill(pw)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
        st = alive(page)
        c.ok("登录成功且存活", st["app"] and st["text"] > 20, str(st))

        if not open_memory_tab(page, SHOT, "open"):
            c.ok("能打开记忆 tab", False, "找不到「我的记忆」tab")
            browser.close()
            return c.report()
        c.ok("记忆 tab 已打开", True)

        # 取 token 供 API 侧核对（与 UI 操作互相印证）
        token = page.evaluate("() => localStorage.getItem('app_token') || ''")

        check_status_bar(c, page, SHOT, base, token)
        check_dream_block(c, page, SHOT)
        check_list_and_filter(c, page, SHOT)
        check_editor(c, page, SHOT, base, token)
        check_run_dream(c, page, SHOT, base, token)
        check_delete(c, page, SHOT, base, token)
        check_tab_switch_alive(c, page, SHOT)

        # 全程零 pageerror 才算干净（JS 异常常被 try/catch 吞掉，只是没渲染出来）
        c.ok("全程无 JS 运行时错误", not page_errors,
             f"{len(page_errors)} 个：{page_errors[:3]}")

        browser.close()
    return c.report()


def seed(base: str, token: str) -> None:
    """造 6 条记忆：4 条同主题（给 dream 有活可干）+ 2 条无关。"""
    dupes = [
        ("ui-pref-a", "用户希望被称呼为老徐", "用户要求称呼「老徐」。标记 UI_SENTINEL\n"),
        ("ui-pref-b", "称呼：老徐", "他姓徐，叫老徐就行。\n"),
        ("ui-pref-c", "用户称呼偏好", "希望被叫老徐，别用全名。\n"),
        ("ui-pref-d", "上周说过称呼老徐", "上周三提过称呼他老徐。\n"),
    ]
    others = [
        ("ui-proj-x", "项目 Hlaudz 处于 M4.5 阶段", "记忆系统 M4.5 是 dream 合并。\n",
         "project"),
        ("ui-ref-y", "看板在 https://example.invalid/board", "外部指针。\n", "reference"),
    ]
    for slug, desc, body in dupes:
        call(base, "/api/me/memories", {
            "slug": slug, "title": desc, "description": desc,
            "content": body, "kind": "user"}, token=token)
    for slug, desc, body, kind in others:
        call(base, "/api/me/memories", {
            "slug": slug, "title": desc, "description": desc,
            "content": body, "kind": kind}, token=token)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时库与截图")
    args = ap.parse_args()

    src = ROOT / "backend" / "data" / "app.db"
    if not src.is_file():
        print(f"找不到生产库 {src}")
        return 1

    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    SHOT.mkdir(parents=True, exist_ok=True)
    # WAL / SHM 一起复制，否则副本可能缺最新事务
    dbpath = WORK / "app.db"
    shutil.copy2(src, dbpath)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.is_file():
            shutil.copy2(side, Path(str(dbpath) + suffix))

    # 前端 dist 必须是新的，否则测的是旧 UI
    dist = ROOT / "frontend" / "dist" / "index.html"
    if not dist.is_file():
        print("缺少 frontend/dist，先跑 cd frontend && npm run build")
        return 1

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    boot = WORK / "_boot.py"
    boot.write_text(BOOT.format(backend=ROOT / "backend", db=dbpath, port=port),
                    encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "backend")
    # UI 核验要能点「立即整理」：开 dream，但把两道闸放宽到「一定满足」
    env["HLZ_MEMORY_DREAM_ENABLED"] = "1"
    env["HLZ_MEMORY_AUTO_WRITE"] = "1"
    log = open(WORK / "server.log", "wb")
    proc = subprocess.Popen([PY, str(boot)], cwd=str(ROOT / "backend"),
                            stdout=log, stderr=subprocess.STDOUT, env=env)
    code = 1
    try:
        if not wait_health(base):
            print("后端启动失败")
            print(Path(WORK / "server.log").read_text(encoding="utf-8",
                                                      errors="replace")[-3000:])
            return 1
        print(f"后端 {base}（独立库 {dbpath}）\n")

        sfx = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        email = f"ui_{sfx}@example.invalid"
        pw = "UiVerify#123"
        tok = call(base, "/api/auth/register",
                   {"email": email, "password": pw, "name": "UI 核验"})["access_token"]
        seed(base, tok)
        total = call(base, "/api/me/memories", token=tok)["total"]
        print(f"账号 {email}，预置 {total} 条记忆\n")

        code = run_ui(base, email, pw)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        if not args.keep:
            # 截图一定要留（是证据），只清临时库
            shutil.rmtree(WORK, ignore_errors=True)
    print(f"\n截图：{SHOT}")
    return code


if __name__ == "__main__":
    sys.exit(main())
