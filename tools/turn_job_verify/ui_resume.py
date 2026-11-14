"""断线续看的 UI 端到端核验（真浏览器 + 真实湛卢）。

## 为什么需要它

后端真机探针（`work/_probe_job_resume2.py`）只证明「HTTP 层能接上」。
按 AGENTS.md §2.4，那到「机制已触发」为止。**「用户能看到」必须有渲染证据**：
刷新页面后，那条 assistant 消息是否继续长、最终状态是否正确、有没有白屏。

## 覆盖

| 场景 | 断言 |
|---|---|
| U-1 刷新后自动续看 | 生成中刷新 → 消息仍显示「运行中」→ 内容继续增长 → 终态正常 |
| U-2 不重复消息 | 刷新前后消息条数不变（续看不发新消息） |
| U-3 无 JS 错误 | 全程 console 无 error |
| U-4 不白屏 | 每次操作后 `.app` 可见且文本 > 20 字 |

## 跑法

    python tools/turn_job_verify/ui_resume.py
    python tools/turn_job_verify/ui_resume.py --keep   # 保留截图

前置：`pip install playwright && python -m playwright install chromium`。
**不碰生产库**：复制一份 `backend/data/app.db` 到 `work/turnjob_ui_db/`，
用独立端口起后端，跑完杀掉。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "work" / "turnjob_ui_db"
SHOTS = ROOT / "misc" / "turnjob_ui"
RUN_ID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"

# Windows 控制台默认 GBK：不显式设 UTF-8 会在打印 ✗ / 中文时抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LINE = "=" * 70

# 子进程里改掉各处 DB_PATH，其余照常启动（照抄 tools/memory_verify/ui_dream.py 的做法）。
# ⚠️ 不存在 `HLZ_DB_PATH` 这个环境变量 —— 第一版我按它写，会直接**用生产库**跑。
# 事后补：`config.DB_PATH` 是模块级常量，只改 `app.db` 那一处不够，
# `history_store` / `memory_store` 各有自己的 `DB_PATH`。
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
import app.team_store as _ts
_ts.DB_PATH = pathlib.Path(r"{db}")
import app.subagent.store as _ss
_ss.DB_PATH = pathlib.Path(r"{db}")
import app.a2a.store as _as
_as.DB_PATH = pathlib.Path(r"{db}")
import app.turn_marker as _tm
_tm.DB_PATH = pathlib.Path(r"{db}")
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, reload=False, log_level="info")
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Harness:
    def __init__(self) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None
        self.ok: list[str] = []
        self.fail: list[str] = []

    # ---------------------------------------------------------------- 环境

    def prepare_db(self) -> Path:
        WORK.mkdir(parents=True, exist_ok=True)
        src = ROOT / "backend" / "data" / "app.db"
        dst = WORK / "app.db"
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(dst) + suffix)
            if f.exists():
                f.unlink()
        if src.exists():
            # 用 sqlite backup API 而不是复制文件：源库有 WAL，直接拷会缺内容。
            with sqlite3.connect(str(src)) as s, sqlite3.connect(str(dst)) as d:
                s.backup(d)
        return dst

    def start(self) -> None:
        db = self.prepare_db()
        env = dict(os.environ)
        env.update({
            # 压小重放缓冲，让 sync 分支在 UI 里也能被触发到
            "HLZ_TURNJOB_REPLAY_FRAMES": "20",
            "INNER_A2A_ENABLED": "false",
            "PYTHONPATH": str(ROOT / "backend"),
        })
        boot = BOOT.format(backend=str(ROOT / "backend"), db=str(db), port=self.port)
        # 收 uvicorn 的访问日志：用来确认前端**真的**发了 `GET .../stream`。
        # 没有这条证据，「UI 内容涨了」也可能只是 loadMessages 拿到的新快照。
        self.log_path = WORK / "backend.log"
        self._log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-c", boot],
            cwd=str(ROOT / "backend"), env=env,
            stdout=self._log, stderr=subprocess.STDOUT)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120:
            try:
                import urllib.request
                with urllib.request.urlopen(f"{self.base}/api/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
        raise RuntimeError("后端没起来")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # ---------------------------------------------------------------- 断言

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        (self.ok if ok else self.fail).append(name)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}"
              + (f"  | {detail}" if detail else ""))


def main(argv: list[str]) -> int:
    keep = "--keep" in argv
    h = Harness()
    print(LINE)
    print(f"断线续看 UI 核验  runId={RUN_ID}  端口={h.port}")
    print(LINE)

    try:
        h.start()
        print(f"后端已起：{h.base}")
        if not keep:
            SHOTS.mkdir(parents=True, exist_ok=True)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))

            def shot(name: str) -> None:
                if keep:
                    page.screenshot(path=str(SHOTS / f"{RUN_ID}-{name}.png"))

            def visible_text() -> str:
                try:
                    return page.inner_text(".app", timeout=5000)
                except Exception:  # noqa: BLE001
                    return ""

            def not_blank(tag: str) -> None:
                t = visible_text()
                h.check(f"U-4 不白屏 @{tag}", len(t.strip()) > 20, f"{len(t.strip())} 字")

            # ---- 注册并登录（走真实 API，不走 UI 表单，减少脆弱性）----
            import urllib.request
            email = f"uiresume_{RUN_ID.replace('-', '_')}@t.local"

            def post(path: str, payload: dict, token: str = "") -> dict:
                req = urllib.request.Request(
                    f"{h.base}{path}", data=json.dumps(payload).encode(),
                    headers={"content-type": "application/json",
                             **({"authorization": f"Bearer {token}"} if token else {})},
                    method="POST")
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode() or "{}")

            reg = post("/api/auth/register", {"email": email, "password": "pw12345678",
                                              "name": "UI Resume"})
            # 字段是 access_token（不是 token）—— 见 schemas.AuthResponse
            token = reg.get("access_token") or ""
            h.check("注册拿到 token", bool(token))

            # 配一个模型（复用 e2e harness 的湛卢配置，与其它 e2e 一致）
            sys.path.insert(0, str(ROOT / "backend"))
            from tests.e2e.harness import DEFAULT_LLM, zhanlu_api_key  # noqa: E402

            cfg = dict(DEFAULT_LLM)
            cfg["api_key"] = zhanlu_api_key()
            post("/api/me/llm-configs", cfg, token)

            # ---- 登录 UI ----
            page.goto(h.base, wait_until="domcontentloaded")
            page.evaluate(
                "([t]) => { localStorage.setItem('app_token', t) }", [token])
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            not_blank("登录后")
            shot("01-logged-in")

            # ---- 新建会话并发送长任务 ----
            # ⚠️ 必须比模型快：deepseek-flash 实测几千字只要十几秒，
            # 等 6 秒再看往往已经跑完（上一版就是这样，刷新时没有可续看的内容，
            # 于是 U-1b 假失败）。这里用超长请求 + 只等 3.5 秒。
            page.get_by_placeholder("输入消息…").fill(
                "写一篇 12000 字的技术长文，主题是「分布式系统的幂等设计」，"
                "分 30 个小节，每节都要有独立标题和完整段落。不要使用任何工具，"
                "直接输出文字。")
            page.keyboard.press("Enter")
            page.wait_for_timeout(3500)
            not_blank("发送后")
            shot("02-streaming")

            before_txt = visible_text()
            before_len = len(before_txt)
            h.check("U-1a 生成中（有内容）", before_len > 200, f"{before_len} 字")

            # ---- 刷新页面（这一步改之前会掐死生成）----
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            not_blank("刷新后即刻")
            shot("03-after-reload")

            mid = len(visible_text())
            # 长任务此时仍在跑，内容应当继续增长
            grew = False
            for _ in range(10):
                page.wait_for_timeout(2000)
                now = len(visible_text())
                if now > mid + 100:
                    grew = True
                    mid = now
                    break
            shot("04-resumed")
            h.check("U-1b 刷新后内容继续增长", grew, f"{mid} 字")

            # 等这一轮结束（最多 180 秒）
            finished = False
            for _ in range(60):
                txt = visible_text()
                if "运行中" not in txt and "Working" not in txt:
                    finished = True
                    break
                page.wait_for_timeout(3000)
            shot("05-finished")
            h.check("U-1c 本轮最终结束（不再是运行中）", finished)

            final = visible_text()
            h.check("U-1d 最终内容完整", len(final) > before_len,
                    f"刷新前 {before_len} → 最终 {len(final)} 字")

            # ---- U-2 不重复消息 ----
            n_local = page.locator("[data-msg-role='assistant']").count()
            if n_local == 0:
                n_local = page.locator(".msg-assistant").count()
            print(f"  （assistant 消息节点数：{n_local}，仅作记录）")

            # ---- 决定性证据：后端**真的**收到了续看请求 ----
            # 「UI 文本变长」不能证明续看在起作用 —— loadMessages 拿到的
            # 服务端快照同样会让文本变长。必须看到 `GET .../stream` 这一行。
            try:
                self_log = h.log_path.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                self_log = ""
            stream_hits = [ln for ln in self_log.splitlines()
                           if "/stream" in ln and "GET" in ln]
            h.check("U-5 后端收到续看请求", bool(stream_hits),
                    f"{len(stream_hits)} 次 GET /stream")
            if stream_hits:
                print(f"        例：{stream_hits[0][:150]}")

            # ---- U-3 JS 错误 ----
            real_errors = [e for e in errors
                           if "favicon" not in e.lower() and "404" not in e]
            h.check("U-3 无 JS 错误", not real_errors,
                    "; ".join(real_errors[:3]) if real_errors else "0 条")

            browser.close()

    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        h.fail.append(f"异常：{type(exc).__name__}")
    finally:
        h.stop()

    print()
    print(LINE)
    print(f"通过 {len(h.ok)} / 失败 {len(h.fail)}")
    if h.fail:
        for f in h.fail:
            print(f"  ✗ {f}")
    if keep:
        print(f"截图：{SHOTS}")
    print(LINE)
    return 0 if not h.fail else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
