"""验证本轮三处 UX 改动（真浏览器 + 真湛卢，隔离库）。

## 背景

`docs/20261115-上下文用量超100%的成因与自动压缩时机核实.md` 定了三件事：

1. 后台软线压缩发生在 `done` 之后，前端只在 `done` 后 180ms 刷一次 `/context`，
   于是页脚会停在压缩前的偏高读数上（实测拍到 `440.8k / 400.0k · 110.2%`）；
2. `compaction` 事件只在赛前路径发，赛后压缩用户完全无感；
3. `/compact` 的「消息太少」只报条数不报 token，反直觉。

对应的改动：

- `ContextUsageOut.compaction_pending` —— 后端如实报告「后台还在压」；
- 前端 `refreshContextUntilSettled` —— 有界轮询到结算为止（最多 10 次 × 6s）；
- 前端页脚夹到 100% + 显示「（整理中…）」；
- `/compact` 提示带 token。

## 断言

- G-1 `/context` 响应里带 `compaction_pending` 字段（契约存在）
- G-2 页脚**从不**显示超过 100% 的百分比（夹取生效）
- G-3 页脚数字与最近一次 `/context` 响应一致（没算错）
- G-4 压缩窗口期内页脚出现过「整理中」，且**之后自动消失**（说明轮询真的在跑）
- G-5 页脚最终数字比窗口期里的峰值小（轮询确实把旧值换掉了）
- G-6 `/compact` 在消息少时给出带 token 的中文说明
- G-7 全程无 JS 错误

## 跑法

    python tools/turn_job_verify/ctx_settle_ui.py --port 8117 --window 40000
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RUN_ID = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"
SHOTS = ROOT / "misc" / "ctx_settle"
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  | {detail}" if detail else ""),
          flush=True)


def post(base, path, payload, token=""):
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(payload).encode(),
        headers={"content-type": "application/json",
                 **({"authorization": f"Bearer {token}"} if token else {})},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode() or "{}")


def main(argv: list[str]) -> int:
    port = 8117
    window = 40000
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if "--window" in argv:
        window = int(argv[argv.index("--window") + 1])
    base = f"http://127.0.0.1:{port}"

    print("=" * 74)
    print(f"页脚结算 UX 验证  runId={RUN_ID}  窗口={window}")
    print("=" * 74, flush=True)

    email = f"settle_{RUN_ID.replace('-', '_')}@t.local"
    token = post(base, "/api/auth/register",
                 {"email": email, "password": "pw12345678",
                  "name": "Settle"}).get("access_token") or ""
    from tests.e2e.harness import zhanlu_api_key
    post(base, "/api/me/llm-configs", {
        "label": "settle", "wire_api": "openai_chat",
        "base_url": "https://ecloud.10086.cn/api/query/aigateway/v1",
        "model": "moma/deepseek-latest-flash",
        "context_window": window, "max_tokens": 8192,
        "is_default": True, "api_key": zhanlu_api_key(),
    }, token)

    from playwright.sync_api import sync_playwright
    ctx_log: list[dict] = []
    js_errors: list[str] = []
    SHOTS.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})

        def on_response(resp):
            if "/context" not in resp.url or resp.request.method != "GET":
                return
            try:
                b = resp.json()
            except Exception:  # noqa: BLE001
                return
            ctx_log.append({"at": time.monotonic(), "body": b})
            print(f"  [CTX] used={b.get('used_tokens')} pct={b.get('percent')} "
                  f"pending={b.get('compaction_pending')}", flush=True)

        page.on("response", on_response)
        page.on("pageerror", lambda e: js_errors.append(str(e)))

        page.goto(base, wait_until="domcontentloaded")
        page.evaluate("([t]) => localStorage.setItem('app_token', t)", [token])
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        def footer() -> str:
            try:
                return page.inner_text(".composer-foot", timeout=3000).replace("\n", " | ")
            except Exception:  # noqa: BLE001
                return ""

        # 页脚文字形如：
        # `Hlaudz 可能会出错，请核对重要信息。 · 41.2k / 40.0k · 100.0%（整理中…） | ...`
        # 第一版直接 `float(f.split(" / ")[0])` —— 前面那一长串中文让 float 抛
        # ValueError，于是「到 85% 停手」从未生效、`peak_pct` 也永远是 0。
        # 这是**判据 bug**，不是产品问题。用正则精确取三段数字。
        import re as _re
        _FOOT = _re.compile(
            r"([\d.]+)k\s*/\s*([\d.]+)k\s*·\s*([\d.]+)%")

        def parse_footer(f: str) -> tuple[float, float, float] | None:
            m = _FOOT.search(f or "")
            if not m:
                return None
            return float(m.group(1)), float(m.group(2)), float(m.group(3))

        # 全局观测：整轮跑下来出现过的最坏/最好看的值，以及有没有见过「整理中」。
        # **必须在每一轮都采样**，不能只在一个窗口里看 —— 「整理中」可能出现在
        # 任何一次跨软线的轮次（实测是充量循环第 6 轮，而不是最后那次发送）。
        seen = {"peak_pct": 0.0, "compacting": False, "compacting_then_gone": False,
                "peak_used_k": 0.0}
        compacting_at: list[str] = []

        def observe(tag: str) -> str:
            f = footer()
            got = parse_footer(f)
            if got is not None:
                used_k, _total_k, pct = got
                seen["peak_pct"] = max(seen["peak_pct"], pct)
                seen["peak_used_k"] = max(seen["peak_used_k"], used_k)
            if "整理中" in f:
                seen["compacting"] = True
                compacting_at.append(tag)
            elif seen["compacting"]:
                seen["compacting_then_gone"] = True
            return f

        # ---------------- G-1 契约字段存在 ----------------
        # 先打一轮短对话，让 /context 至少被请求一次
        page.get_by_placeholder("输入消息…").fill("用一句话解释什么是幂等。不要用工具。")
        page.keyboard.press("Enter")
        page.wait_for_timeout(20000)

        g1 = any("compaction_pending" in c["body"] for c in ctx_log)
        record("G-1 /context 响应带 compaction_pending 字段", g1,
               f"{len(ctx_log)} 次响应")

        # ---------------- 充到软线以上 ----------------
        big = ("写一篇 12000 字的技术长文，主题自选，分 24 个小节，"
               "每节独立标题和完整段落。不要用工具。")
        for turn in range(1, 15):
            page.get_by_placeholder("输入消息…").fill(big)
            page.keyboard.press("Enter")
            # 等这一轮结束（停止按钮消失）
            for _ in range(240):
                page.wait_for_timeout(1000)
                if page.locator("button.send-icon.stop").count() == 0:
                    break
            # 结束后立刻多采几次：赛后压缩在 done 之后才启动，页脚会在这个
            # 窗口里出现「整理中」和 >100% 的真实读数（夹取前）。
            for _ in range(6):
                f = observe(f"第{turn}轮")
                page.wait_for_timeout(1500)
            print(f"  第 {turn} 轮后页脚：{f[:96]}", flush=True)
            got = parse_footer(f)
            if got is None:
                continue
            used_k, total_k, _pct = got
            if total_k > 0 and used_k / total_k >= 0.85:
                print(f"  ↑ 已达 {used_k / total_k:.1%}，停手观察", flush=True)
                break

        # ---------------- 再发一条大的，跨过软线 ----------------
        page.get_by_placeholder("输入消息…").fill(big)
        page.keyboard.press("Enter")
        # 发出去之后立刻开采样：跨软线的那一轮结束 → 赛后压缩启动，
        # 「整理中」和夹取前的 >100% 都只在这个窗口里可见。
        for i in range(120):
            page.wait_for_timeout(1000)
            observe(f"跨线观察{i}s")
            if page.locator("button.send-icon.stop").count() == 0 and i > 25:
                # 本轮已结束，再给后台压缩 60 秒结算时间
                if i > 85:
                    break

        page.screenshot(path=str(SHOTS / f"{RUN_ID}-footer.png"))
        final_footer = observe("最终")
        peak_pct = seen["peak_pct"]

        # ---------------- G-2 页脚从不显示 >100% ----------------
        # 只看「最大显示 ≤100」是空断言：充不到软线时它也成立。
        # 必须有**后端真报过 >100%** 而**页脚仍不显示 >100%**，才证明夹取生效。
        raw_over = [c for c in ctx_log if (c["body"].get("percent") or 0) > 100]
        record("G-2 页脚从不显示超过 100%（后端确实报过 >100% 才作数）",
               peak_pct <= 100.0 and bool(raw_over),
               f"后端 >100% 共 {len(raw_over)} 次"
               + (f"（最大 {max(c['body']['percent'] for c in raw_over)}%）" if raw_over else "（未触发，判据不成立）")
               + f"；页脚最大显示 {peak_pct}%")

        # ---------------- G-3 页脚与 /context 一致 ----------------
        consistent = False
        detail3 = "没有可对照的 /context"
        if ctx_log and final_footer:
            last = ctx_log[-1]["body"]
            used = last.get("used_tokens") or 0
            want = f"{min(100.0, used / max(1, last.get('context_window') or 1) * 100):.1f}"
            consistent = want in final_footer
            detail3 = f"/context used={used} → 期望 {want}% ；页脚《{final_footer[:70]}》"
        record("G-3 页脚数字与最近一次 /context 一致", consistent, detail3)

        # ---------------- G-4 出现过「整理中」且之后消失 ----------------
        record("G-4 压缩窗口期内页脚出现「整理中」提示", seen["compacting"],
               ("见到：" + "、".join(compacting_at[:4])) if seen["compacting"]
               else "全程没见到——可能没跨过软线，或 pending 恒 false")
        record("G-4b 「整理中」之后自动消失（轮询把旧值换掉了）",
               seen["compacting_then_gone"] and "整理中" not in final_footer,
               f"最终页脚《{final_footer[:70]}》")

        # ---------------- G-5 峰值之后下降 ----------------
        if ctx_log:
            peak_used = max((c["body"].get("used_tokens") or 0) for c in ctx_log)
            last_used = ctx_log[-1]["body"].get("used_tokens") or 0
            record("G-5 最终用量低于观察期峰值（后台压缩确实完成）",
                   last_used < peak_used,
                   f"峰值 {peak_used} → 最终 {last_used}")
        else:
            record("G-5 最终用量低于观察期峰值（后台压缩确实完成）", False, "没有 /context 记录")

        # ---------------- G-6 /compact 提示带 token ----------------
        page.get_by_placeholder("输入消息…").fill("/compact")
        page.keyboard.press("Enter")
        page.wait_for_timeout(6000)
        body_txt = page.inner_text(".msg-list" if page.locator(".msg-list").count()
                                   else "body", timeout=5000)
        has_tokens = "tokens" in body_txt
        record("G-6 /compact 说明里带 token 数", has_tokens,
               ("出现 tokens" if has_tokens else "未见 tokens（可能是消息数已超阈值，走了真压缩）"))
        page.screenshot(path=str(SHOTS / f"{RUN_ID}-compact.png"))

        # ---------------- G-7 无 JS 错误 ----------------
        record("G-7 全程无 JS 错误", not js_errors,
               f"{len(js_errors)} 条" + (f"：{js_errors[0][:120]}" if js_errors else ""))

        browser.close()

    failed = [n for n, ok, _ in results if not ok]
    print()
    print(f"通过 {len(results) - len(failed)} / 失败 {len(failed)}")
    for n in failed:
        print(f"  ✗ {n}")

    out = ROOT / "backend" / "tests" / "e2e" / "runs" / f"{RUN_ID}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(
        {"run_id": RUN_ID, "window": window, "peak_pct": peak_pct,
         "ctX_responses": ctx_log, "final_footer": final_footer,
         "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"现场：{out}")
    print(f"截图：{SHOTS}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
