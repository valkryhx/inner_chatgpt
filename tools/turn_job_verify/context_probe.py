"""抓前端实际收到的 `/context` 响应。

## 为什么需要它

UI 截图里页脚显示 `440.8k / 400.0k · 110.2%`，但：

- 后端对同一会话返回 `used=86 / window=400000 / 0.02%`（已实测）；
- 库里 155 个会话的 history 逐个算过，最大只有 8889 tokens。

两边都拿不出 440800。所以要么是前端拿到了别的东西，要么是显示层算错。
**不能在猜**——本脚本用 Playwright 监听网络，把 `/context` 的原始响应体打出来，
并且在同一时刻截图页脚文本，两边对照。

## 判据

- N-1 捕获到至少一次 `/context` 响应；
- N-2 响应里的 `used_tokens` 与页脚显示的 X.Xk 一致（不一致 = 显示层 bug）；
- N-3 若响应正常而页脚异常 → 前端状态串了（记下是哪次切换导致）。

## 跑法

    python tools/turn_job_verify/context_probe.py --port 8104
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


def main(argv: list[str]) -> int:
    port = 8104
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    base = f"http://127.0.0.1:{port}"

    print("=" * 72)
    print(f"/context 抓包  runId={RUN_ID}  base={base}")
    print("=" * 72, flush=True)

    def post(path, payload, token=""):
        req = urllib.request.Request(
            f"{base}{path}", data=json.dumps(payload).encode(),
            headers={"content-type": "application/json",
                     **({"authorization": f"Bearer {token}"} if token else {})},
            method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode() or "{}")

    email = f"ctxp_{RUN_ID.replace('-', '_')}@t.local"
    token = post("/api/auth/register",
                 {"email": email, "password": "pw12345678", "name": "CtxProbe"}
                 ).get("access_token") or ""
    from tests.e2e.harness import DEFAULT_LLM, zhanlu_api_key
    cfg = dict(DEFAULT_LLM)
    cfg["api_key"] = zhanlu_api_key()
    post("/api/me/llm-configs", cfg, token)

    from playwright.sync_api import sync_playwright
    captured: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})

        def on_response(resp):
            if "/context" in resp.url:
                try:
                    body = resp.json()
                except Exception:  # noqa: BLE001
                    body = {"_unparsed": True}
                captured.append({"url": resp.url, "status": resp.status, "body": body})
                print(f"  [NET] {resp.status} {resp.url.split('/api')[-1]} -> "
                      f"used={body.get('used_tokens')} win={body.get('context_window')} "
                      f"pct={body.get('percent')}", flush=True)

        page.on("response", on_response)
        page.goto(base, wait_until="domcontentloaded")
        page.evaluate("([t]) => localStorage.setItem('app_token', t)", [token])
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        def footer() -> str:
            try:
                return page.inner_text(".composer-foot", timeout=3000)
            except Exception:  # noqa: BLE001
                return ""

        print(f"  初始页脚：{footer()!r}", flush=True)

        # 发一条短消息，制造用量
        page.get_by_placeholder("输入消息…").fill("用一句话解释幂等。不要用工具。")
        page.keyboard.press("Enter")
        page.wait_for_timeout(9000)
        print(f"  一轮后页脚：{footer()!r}", flush=True)

        # 新建第二个会话（这里最可能串状态）
        try:
            page.locator("button.side-nav-item", has_text="新建").first.click(timeout=3000)
            page.wait_for_timeout(2500)
            print(f"  新建会话后页脚：{footer()!r}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  新建失败：{e}", flush=True)

        # 再发一条
        page.get_by_placeholder("输入消息…").fill("用一句话解释 CAP。不要用工具。")
        page.keyboard.press("Enter")
        page.wait_for_timeout(9000)
        print(f"  第二会话一轮后页脚：{footer()!r}", flush=True)

        # 额外：长任务生成中读取页脚（截图里 440.8k 就出现在这种时刻）
        page.get_by_placeholder("输入消息…").fill(
            "输出从 1 到 300 的编号列表，每行「第N项」。全部输出，不要用工具。")
        page.keyboard.press("Enter")
        for i in range(6):
            page.wait_for_timeout(1500)
            print(f"  生成中[{i}] 页脚：{footer()!r}", flush=True)
            try:
                aid = page.evaluate("() => localStorage.getItem('app_token') ? 1 : 0")
            except Exception:
                aid = -1
        page.screenshot(path=str(ROOT / "misc" / "ui_acceptance" / f"{RUN_ID}-ctxprobe.png"))
        browser.close()

    print()
    print(f"共捕获 {len(captured)} 次 /context 响应")
    ok = len(captured) > 0
    print(f"N-1 捕获到 /context 响应: {'OK' if ok else 'FAIL'}")
    if captured:
        print("\n所有响应：")
        for c in captured:
            b = c["body"]
            print(f"  {c['status']} used={b.get('used_tokens')} "
                  f"win={b.get('context_window')} pct={b.get('percent')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
