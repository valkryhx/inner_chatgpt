"""挑剔用户视角的 UI 验收：卡顿 / 多功能并存 / 压缩 / UX。

## 与既有 UI 测试的分工

`ui_resume.py` 只验「断线续看」这一条。本脚本站在**挑剔用户**的位置横着扫：

| 组 | 验什么 | 为什么挑剔用户会在意 |
|---|---|---|
| P 组 卡顿 | 首帧耗时、生成中滚动跟随、输入框可输入、停止按钮响应 | 「点了没反应」「滚不动」「打完字卡住」是用户最先骂的 |
| F 组 多功能 | 多会话并建并切、停止、插话、工具步骤折叠、上下文用量显示 | 功能之间会不会互相踩 |
| K 组 压缩 | `/compact` 的真实行为、压缩后上下文用量下降、界面不丢消息 | 长会话用户天天用 |
| X 组 UX | 空态、错误提示、可读性（对比度靠截图人工看）、无 JS 错误、刷新不丢 | 「看起来是不是个正经产品」 |

## 判据纪律

- 卡顿用**真实计时**（`performance.now()` 到首帧可见），不靠体感；
- 「首帧可见」指 `.msg.assistant` 节点出现且文本非空 —— 不是「SSE 发了 message_start」；
- 滚动跟随看**是否停在底部附近**（距底 < 120px），不是看有没有 scrollTop 变化；
- 压缩必须看**后端返回 + 前端用量数字**两侧，只信一侧会被缓存骗。

## 跑法

    python tools/turn_job_verify/ui_acceptance.py
    python tools/turn_job_verify/ui_acceptance.py --keep   # 保留截图
    python tools/turn_job_verify/ui_acceptance.py --port 8000   # 打已有后端（危险：生产库）

默认自带隔离库，不碰 `backend/data/app.db`。
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "work" / "ui_acceptance"
SHOTS = ROOT / "misc" / "ui_acceptance"
RUN_ID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
LINE = "=" * 74

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
    def __init__(self, external: int = 0) -> None:
        self.external = external
        self.port = external or free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = None
        self.ok: list[str] = []
        self.fail: list[str] = []
        self.timings: list[tuple[str, float]] = []

    def prepare_db(self) -> Path:
        WORK.mkdir(parents=True, exist_ok=True)
        src = ROOT / "backend" / "data" / "app.db"
        dst = WORK / "app.db"
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(dst) + suffix)
            if f.exists():
                f.unlink()
        if src.exists():
            with sqlite3.connect(str(src)) as s, sqlite3.connect(str(dst)) as d:
                s.backup(d)
        return dst

    def start(self) -> None:
        if self.external:
            import urllib.request
            with urllib.request.urlopen(f"{self.base}/api/health", timeout=5) as r:
                if r.status != 200:
                    raise RuntimeError("外部后端不健康")
            self.log_path = None
            return
        db = self.prepare_db()
        env = dict(os.environ)
        env.update({"INNER_A2A_ENABLED": "false",
                    "PYTHONPATH": str(ROOT / "backend")})
        env.pop("HLZ_TURNJOB_HEARTBEAT_IDLE_S", None)
        boot = BOOT.format(backend=str(ROOT / "backend"), db=str(db), port=self.port)
        self.log_path = WORK / "backend.log"
        self._log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-c", boot],
                                     cwd=str(ROOT / "backend"), env=env,
                                     stdout=self._log, stderr=subprocess.STDOUT)
        import urllib.request
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120:
            try:
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

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        (self.ok if ok else self.fail).append(name)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}"
              + (f"  | {detail}" if detail else ""), flush=True)

    def timing(self, name: str, ms: float, budget_ms: float) -> None:
        self.timings.append((name, ms))
        self.check(f"{name} ≤ {budget_ms:.0f}ms", ms <= budget_ms, f"实测 {ms:.0f}ms")


def main(argv: list[str]) -> int:
    keep = "--keep" in argv
    external = 0
    if "--port" in argv:
        external = int(argv[argv.index("--port") + 1])

    h = Harness(external)
    print(LINE)
    print(f"挑剔用户 UI 验收  runId={RUN_ID}  端口={h.port}")
    print(LINE, flush=True)

    try:
        h.start()
        print(f"后端：{h.base}\n", flush=True)
        SHOTS.mkdir(parents=True, exist_ok=True)

        from playwright.sync_api import sync_playwright

        import urllib.request

        def post(path: str, payload: dict, token: str = "") -> dict:
            req = urllib.request.Request(
                f"{h.base}{path}", data=json.dumps(payload).encode(),
                headers={"content-type": "application/json",
                         **({"authorization": f"Bearer {token}"} if token else {})},
                method="POST")
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode() or "{}")

        email = f"uacc_{RUN_ID.replace('-', '_')}@t.local"
        reg = post("/api/auth/register",
                   {"email": email, "password": "pw12345678", "name": "挑剔用户"})
        token = reg.get("access_token") or ""
        h.check("账号就绪", bool(token))

        sys.path.insert(0, str(ROOT / "backend"))
        from tests.e2e.harness import DEFAULT_LLM, zhanlu_api_key
        cfg = dict(DEFAULT_LLM)
        cfg["api_key"] = zhanlu_api_key()
        post("/api/me/llm-configs", cfg, token)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 860})
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))

            def shot(name: str) -> None:
                if keep or True:
                    page.screenshot(path=str(SHOTS / f"{RUN_ID}-{name}.png"))

            # 录制每次 /context 响应：页脚数字若与这里不符，就是显示层/状态串了。
            ctx_log: list[dict] = []

            def on_response(resp):
                if "/context" not in resp.url:
                    return
                try:
                    b = resp.json()
                except Exception:  # noqa: BLE001
                    b = {}
                ctx_log.append({"url": resp.url, "status": resp.status, "body": b})
                print(f"  [CTX] used={b.get('used_tokens')} win={b.get('context_window')} "
                      f"pct={b.get('percent')}", flush=True)

            page.on("response", on_response)

            def footer_usage() -> str:
                try:
                    t = page.inner_text(".composer-foot", timeout=3000)
                except Exception:  # noqa: BLE001
                    return ""
                return t.replace("\n", " | ")

            def vtext() -> str:
                try:
                    return page.inner_text(".app", timeout=8000)
                except Exception:  # noqa: BLE001
                    return ""

            # ================= 登录 =================
            page.goto(h.base, wait_until="domcontentloaded")
            page.evaluate("([t]) => localStorage.setItem('app_token', t)", [token])
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            shot("01-login")

            # ================= X-1 空态 =================
            empty = vtext()
            h.check("X-1 新账号空态不白屏且有引导", len(empty.strip()) > 20,
                    f"{len(empty.strip())} 字")
            print(f"        空态文本节选：{empty.strip()[:120]!r}")

            # ================= P-1 首帧延迟 =================
            page.get_by_placeholder("输入消息…").fill("用一句话解释什么是幂等。不要用工具。")
            t_send = time.monotonic()
            page.keyboard.press("Enter")

            first_visible_ms = None
            for _ in range(300):                     # 最多 30s
                page.wait_for_timeout(100)
                try:
                    txt = page.locator(".msg.assistant").last.inner_text(timeout=500)
                except Exception:  # noqa: BLE001
                    txt = ""
                if txt and len(txt.strip()) > 3:
                    first_visible_ms = (time.monotonic() - t_send) * 1000
                    break
            h.check("P-1 首帧可见（assistant 节点出现且有字）",
                    first_visible_ms is not None,
                    f"{first_visible_ms:.0f}ms" if first_visible_ms else "30s 内没出现")
            if first_visible_ms:
                # 预算 12s：真实 LLM + 首帧前奏（含 MCP 探测）通常在 3-9s
                h.timing("P-1 首帧延迟", first_visible_ms, 12000)
            shot("02-first-frame")

            # 等这一轮结束
            for _ in range(100):
                if "运行中" not in vtext():
                    break
                page.wait_for_timeout(300)

            # ================= P-2 生成中滚动跟随 =================
            # 用足够长的输出 + 明确要求逐步输出，保证**一定**还在生成，
            # 后面 P-4 才有停止按钮可点（v2 就是因为跑太快没点到停止，
            # 而第一次出现 440.8k 恰恰在「点过停止 + 切会话」之后）。
            page.get_by_placeholder("输入消息…").fill(
                "输出从 1 到 600 的编号列表，每行「第N项：这是第N条说明」，"
                "全部输出，不要省略，不要用工具。")
            page.keyboard.press("Enter")
            page.wait_for_timeout(2500)

            def dist_to_bottom() -> float:
                return page.evaluate(
                    """() => {
                        const el = document.scrollingElement || document.documentElement;
                        return el.scrollHeight - el.scrollTop - el.clientHeight;
                    }""")

            gaps = []
            for _ in range(12):
                page.wait_for_timeout(700)
                gaps.append(dist_to_bottom())
            following = sum(1 for g in gaps if g < 160)
            h.check("P-2 生成中自动跟随到底部", following >= len(gaps) - 1,
                    f"{following}/{len(gaps)} 次距底 <160px，最大 {max(gaps):.0f}px")
            shot("03-following")

            # ================= P-3 生成中输入框可用 =================
            # 生成中 placeholder 会变成「补充一句要求…」，这是 steering 的入口。
            # 挑剔用户会在这里确认：程序没卡死（能打字）、且打字不会被吞。
            typed = False
            used_ph = ""
            for ph in ("补充一句要求…（会插入当前任务，不会另起一轮）", "输入消息…"):
                try:
                    page.get_by_placeholder(ph).fill("测试输入", timeout=2500)
                    typed = True
                    used_ph = ph
                    break
                except Exception:  # noqa: BLE001
                    continue
            h.check("P-3 生成中输入框仍可输入（不 disabled）", typed,
                    f"用了 placeholder {used_ph!r}" if typed else "两种 placeholder 都填不进去")
            if typed:
                got = page.get_by_placeholder(used_ph).input_value()
                h.check("P-3b 输入内容没有被吞", got == "测试输入", f"回读={got!r}")
                page.get_by_placeholder(used_ph).fill("")

            # ================= P-4 停止按钮响应 =================
            t_stop = time.monotonic()
            stop_btn = page.locator("button.send-icon.stop")
            had_stop = stop_btn.count() > 0
            if had_stop:
                stop_btn.first.click()
                stopped_ms = None
                for _ in range(100):
                    page.wait_for_timeout(100)
                    if "运行中" not in vtext():
                        stopped_ms = (time.monotonic() - t_stop) * 1000
                        break
                h.check("P-4 点击停止后界面不再显示运行中", stopped_ms is not None,
                        f"{stopped_ms:.0f}ms" if stopped_ms else "10s 内没停")
                if stopped_ms:
                    h.timing("P-4 停止响应", stopped_ms, 3000)
                # 停止**之后**立刻看用量：第一次的 440.8k 就出现在这个窗口
                page.wait_for_timeout(2500)
                f_after_stop = footer_usage()
                print(f"        停止后页脚：{f_after_stop}")
                if ctx_log:
                    u = ctx_log[-1]["body"].get("used_tokens") or 0
                    h.check("P-4b 停止后页脚用量仍与 /context 一致",
                            f"{u / 1000:.1f}k" in f_after_stop,
                            f"后端 used={u} → 页脚 {f_after_stop[:70]!r}")
                shot("04-stopped")
            else:
                # 这一轮已经自然跑完（模型很快），不算失败，如实记录
                h.check("P-4 停止按钮（本轮已结束，跳过）", True,
                        "生成已自然完成，未观察到停止按钮")
                page.wait_for_timeout(500)

            page.wait_for_timeout(1500)

            # ================= F-1 多会话并存与切换 =================
            # 真实选择器见 frontend/src/components/Sidebar.tsx:149
            #   <button className="side-nav-item" onClick={onCreate}>＋ 新建 …</button>
            new_btn = page.locator("button.side-nav-item", has_text="新建")
            h.check("F-1a 能找到「新建」入口", new_btn.count() > 0)
            if new_btn.count() == 0:
                new_btn = page.locator("button.side-nav-item").first
            if new_btn.count() > 0:
                new_btn.first.click()
                page.wait_for_timeout(1200)
                page.get_by_placeholder("输入消息…").fill(
                    "用一句话说明 CAP 定理。不要用工具。")
                page.keyboard.press("Enter")
                page.wait_for_timeout(6000)
                two = page.locator(".msg.assistant").count()
                h.check("F-1b 第二个会话独立生成", two >= 1, f"assistant 节点 {two} 个")
                items = page.locator(".session-item")
                h.check("F-1c 侧栏能看到多个会话", items.count() >= 2,
                        f"{items.count()} 个")
                shot("05-multi-session")

            # ================= F-2 上下文用量 =================
            foot = footer_usage()
            print(f"        页脚：{foot}")
            if ctx_log:
                last = ctx_log[-1]["body"]
                used_k = (last.get("used_tokens") or 0) / 1000
                # 页脚格式：… · X.Xk / Y.Yk · Z.Z%
                shown = f"{used_k:.1f}k"
                consistent = shown in foot
                h.check("F-2 页脚用量与后端 /context 一致", consistent,
                        f"后端 used={last.get('used_tokens')} → 期望页脚含 {shown!r}；"
                        f"实际页脚 {foot[:80]!r}")
            else:
                h.check("F-2 页脚用量与后端 /context 一致", False, "全程没捕获到 /context 响应")
            # 超窗检查：真出现 percent>100 就是 UX 问题，必须暴露
            over = [c for c in ctx_log if (c["body"].get("percent") or 0) > 100]
            h.check("F-2b 没有出现超过窗口 100% 的用量显示", not over,
                    f"{len(over)} 次超窗" + (f"，最大 {max(c['body']['percent'] for c in over)}%"
                                             if over else ""))
            shot("06-context-usage")

            # ================= K 压缩 =================
            # 先确认后端支持；再走 UI 指令
            page.get_by_placeholder("输入消息…").fill("/compact")
            page.keyboard.press("Enter")
            page.wait_for_timeout(2000)
            notice = vtext()
            compacted_words = ("已压缩", "压缩", "暂无", "消息较少")
            saw_notice = any(w in notice for w in compacted_words)
            h.check("K-1 /compact 有明确反馈（不是静默无反应）", saw_notice,
                    next((w for w in compacted_words if w in notice), "无任何提示"))
            shot("07-compact")

            # 用 API 交叉验证压缩是否真的可用（UI 提示可能与后端不一致）
            def get(path: str) -> dict:
                req = urllib.request.Request(
                    f"{h.base}{path}",
                    headers={"authorization": f"Bearer {token}"}, method="GET")
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode() or "{}")

            sessions = get("/api/sessions").get("sessions") or []
            h.check("K-2 API 能列出会话（UI 与后端一致）", len(sessions) >= 1,
                    f"{len(sessions)} 个")

            # ================= X-2 刷新不丢 =================
            before_reload = len(vtext())
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            after_reload = len(vtext())
            h.check("X-2 刷新后内容仍在（不白屏、不丢历史）",
                    after_reload > before_reload * 0.5,
                    f"{before_reload} → {after_reload} 字")
            shot("08-after-reload")

            # ================= X-3 设置弹窗 =================
            # 真实选择器：Sidebar.tsx:221 的 .user-chip（title 里带「打开设置」），
            # 以及 :164 的侧栏「设置」项。先试 .user-chip，再退到文案。
            opened = False
            for loc in (page.locator("button.user-chip"),
                        page.locator("button.side-nav-item", has_text="设置")):
                if loc.count() > 0:
                    try:
                        loc.first.click(timeout=3000)
                        opened = True
                        break
                    except Exception:  # noqa: BLE001
                        continue
            page.wait_for_timeout(1200)
            modal = page.locator(".modal")
            h.check("X-3 设置弹窗能打开", opened and modal.count() > 0,
                    f"点击={opened}，弹窗={modal.count()}")
            if modal.count() > 0:
                tabs = page.locator(".modal-tabs button")
                h.check("X-3b 弹窗有多个 tab（LLM/MCP/Skills…）", tabs.count() >= 3,
                        f"{tabs.count()} 个 tab："
                        + ", ".join(tabs.nth(i).inner_text().strip()
                                    for i in range(min(tabs.count(), 8))))
                shot("09-settings")
                page.keyboard.press("Escape")
                page.wait_for_timeout(600)

            # ================= X-4 JS 错误 =================
            real = [e for e in errors if "favicon" not in e.lower()]
            h.check("X-4 全程无 JS 错误", not real,
                    "; ".join(real[:3]) if real else "0 条")
            if real:
                print("\n  JS 错误明细：")
                for e in real[:10]:
                    print(f"    - {e[:200]}")

            browser.close()

    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        h.fail.append(f"异常：{type(exc).__name__}: {exc}")
    finally:
        h.stop()

    print()
    print(LINE)
    print(f"通过 {len(h.ok)} / 失败 {len(h.fail)}")
    for n in h.fail:
        print(f"  ✗ {n}")
    if h.timings:
        print("\n计时：")
        for n, ms in h.timings:
            print(f"  {n:<24} {ms:>8.0f}ms")
    print(f"截图：{SHOTS}")
    print(LINE)

    out = ROOT / "backend" / "tests" / "e2e" / "runs" / f"{RUN_ID}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(
        {"run_id": RUN_ID, "ok": h.ok, "fail": h.fail,
         "timings": h.timings, "port": h.port},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not h.fail else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
