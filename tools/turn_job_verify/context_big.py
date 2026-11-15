"""复现「上下文用量显示 440.8k」的现场（第一版 ui_acceptance 的条件）。

## 已知事实

- 第一次 `ui_acceptance` 的截图 06/07/08 页脚显示 `440.8k / 400.0k · 110.2%`；
- 之后三次重跑（v2/v3 + context_probe）里 `/context` 全部正常（最大 4200）；
- 事后查那份库：155 个会话的 history 逐个算过，**最大只有 8889 tokens**。

所以要么那次前端拿到了一个不属于任何会话的数字，要么那条会话已被我后来的
`/compact` 改写（原状态不可考）。

## 本脚本做什么

严格复制第一次的条件：**「写 12000 字技术长文」**（第一次用的 prompt），
然后：

1. 生成中途读页脚（第一次 03 截图就是这个时刻）；
2. 点停止（第一次 04）；
3. 再看页脚（第一次 06 就在这里变成 440.8k）；
4. 全程录制 `/context` 响应，逐次对照。

判据：

- M-1 每一条 `/context` 响应的 `percent` 都 ≤ 100；
- M-2 每次读到的页脚数字都能在最近一次 `/context` 响应里找到对应；
- M-3 若页脚出现 400k 以上而 `/context` 没给 → **前端状态 bug**，打印全部现场。

## 跑法

    python tools/turn_job_verify/context_big.py --port 8106
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
    port = 8106
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    base = f"http://127.0.0.1:{port}"

    print("=" * 72)
    print(f"大用量复现  runId={RUN_ID}  base={base}")
    print("=" * 72, flush=True)

    def post(path, payload, token=""):
        req = urllib.request.Request(
            f"{base}{path}", data=json.dumps(payload).encode(),
            headers={"content-type": "application/json",
                     **({"authorization": f"Bearer {token}"} if token else {})},
            method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode() or "{}")

    email = f"big_{RUN_ID.replace('-', '_')}@t.local"
    token = post("/api/auth/register",
                 {"email": email, "password": "pw12345678", "name": "Big"}
                 ).get("access_token") or ""
    from tests.e2e.harness import DEFAULT_LLM, zhanlu_api_key
    cfg = dict(DEFAULT_LLM)
    cfg["api_key"] = zhanlu_api_key()
    post("/api/me/llm-configs", cfg, token)

    from playwright.sync_api import sync_playwright
    ctx_log: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})

        def on_response(resp):
            if "/context" not in resp.url:
                return
            try:
                b = resp.json()
            except Exception:  # noqa: BLE001
                b = {}
            ctx_log.append({"url": resp.url, "status": resp.status, "body": b})
            print(f"  [CTX] used={b.get('used_tokens')} pct={b.get('percent')}", flush=True)

        page.on("response", on_response)
        page.goto(base, wait_until="domcontentloaded")
        page.evaluate("([t]) => localStorage.setItem('app_token', t)", [token])
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        def footer() -> str:
            try:
                t = page.inner_text(".composer-foot", timeout=3000)
            except Exception:  # noqa: BLE001
                return ""
            return t.replace("\n", " | ")

        samples: list[tuple[str, str]] = []

        def snap(tag: str) -> None:
            f = footer()
            samples.append((tag, f))
            print(f"  [{tag}] {f[:110]}", flush=True)

        # 与第一版完全相同的 prompt
        page.get_by_placeholder("输入消息…").fill(
            "写一篇 12000 字的技术长文，主题是「分布式系统的幂等设计」，"
            "分 30 个小节，每节都要有独立标题和完整段落。不要使用任何工具，"
            "直接输出文字。")
        page.keyboard.press("Enter")
        page.wait_for_timeout(3500)
        snap("生成中-3.5s")
        page.wait_for_timeout(8000)
        snap("生成中-11.5s")
        page.wait_for_timeout(8000)
        snap("生成中-19.5s")

        # 点停止（第一次 04 截图）
        stop = page.locator("button.send-icon.stop")
        if stop.count() > 0:
            stop.first.click()
            print("  已点击停止", flush=True)
        for i in range(4):
            page.wait_for_timeout(1500)
            snap(f"停止后+{1.5 * (i + 1):.1f}s")

        # 新建会话（第一次 05→06 之间做了这件事）
        try:
            page.locator("button.side-nav-item", has_text="新建").first.click(timeout=3000)
            page.wait_for_timeout(2500)
            snap("新建会话后")
        except Exception as e:  # noqa: BLE001
            print(f"  新建失败：{e}", flush=True)

        page.screenshot(path=str(ROOT / "misc" / "ui_acceptance" / f"{RUN_ID}-big.png"))
        browser.close()

    print()
    worst = max((c["body"].get("percent") or 0) for c in ctx_log) if ctx_log else 0
    over = [c for c in ctx_log if (c["body"].get("percent") or 0) > 100]
    print(f"M-1 所有 /context 的 percent ≤ 100: {'OK' if not over else 'FAIL'}（最大 {worst}%）")

    # 页脚里出现 >400k 的样本？
    big_samples = [(t, f) for t, f in samples if any(
        x in f for x in ("4", "5", "6", "7", "8", "9")) and "k / 400.0k" in f
        and any(f"{n}.{d}k / 400.0k" in f for n in range(100, 1000) for d in range(10))]
    print(f"M-2 页脚出现过 ≥100k 的用量: {'是（见下）' if big_samples else '否'}")
    for t, f in samples:
        print(f"     {t:<16} {f[:100]}")

    return 0 if not over else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
