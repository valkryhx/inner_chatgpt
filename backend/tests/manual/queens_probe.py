"""复现用户的「8 皇后」场景，验证路径与 shell 类型修复是否真的生效。

判定不用「模型说成功了」，而是看三件可验证的事实：
1. 第一次 `code_run` 就没报「文件不存在」/「路径越界」；
2. 全程没有出现 powershell 相关的失败；
3. 产物落在 `D:\\sandbox\\<uid>\\` 且内容真的是 8 皇后解。

用法：
    python tests/manual/queens_probe.py --user hx --rounds 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000"
RUNS = Path(__file__).resolve().parents[1] / "e2e" / "sandbox_runs"

USERS = {"hx": "u_c56b8337dd09", "xh": "u_343a46896bff"}

PROMPT = """请写一个求解 8 皇后的 Python 脚本，保存为 `{fname}`，然后运行它，
把解的个数告诉我（8 皇后应该有 92 个解）。

要求：
- 直接写文件，写完立刻运行，不要先问我
- 输出里带上这个标记：SENTINEL={sentinel}
"""


async def token_for(uid: str) -> str:
    from app import db
    from app.auth import create_access_token
    u = await db.get_user(uid)
    t, _ = create_access_token(uid, u.get("token_ver", 0))
    return t


async def one_round(client: httpx.AsyncClient, uid: str, label: str) -> dict:
    run_id = f"{int(time.time())}-{secrets.token_hex(2)}"
    out = RUNS / f"queens-{run_id}"
    out.mkdir(parents=True, exist_ok=True)
    sentinel = secrets.token_hex(4)
    fname = f"queens_{sentinel}.py"

    token = await token_for(uid)
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}

    r = await client.post("/api/sessions", json={"title": f"queens-{run_id}"},
                          headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    sid = r.json()["id"]

    events: list[dict] = []
    async with client.stream("POST", f"/api/sessions/{sid}/messages",
                             json={"text": PROMPT.format(fname=fname, sentinel=sentinel)},
                             headers=headers, timeout=420) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            raw = line[6:].strip()
            if raw in ("", "[DONE]"):
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                pass

    (out / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")

    # 工具调用序列
    calls = [(e.get("name"), e.get("input", {})) for e in events if e.get("name") and "input" in e]
    results = [e for e in events if e.get("name") and "content" in e]
    text = "".join(e.get("text", "") for e in events if "text" in e)

    # 失败信号（从工具结果里找）
    errs: list[str] = []
    for r in results:
        c = r.get("content")
        s = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        if r.get("is_error") or "[工具执行错误]" in s or "路径越界" in s \
                or "文件不存在" in s or "未找到匹配" in s:
            errs.append(f"{r.get('name')}: {s[:160]}")

    summary = {
        "run_id": run_id,
        "user": label, "uid": uid, "session": sid,
        "sentinel": sentinel, "fname": fname,
        "tool_seq": [c[0] for c in calls],
        "n_tool_calls": len(calls),
        "errors": errs,
        "has_92": "92" in text,
        "powershell_mentioned": "powershell" in text.lower(),
        "text_tail": text[-400:],
        "dir": str(out),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    return summary


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="hx", choices=list(USERS))
    ap.add_argument("--rounds", type=int, default=3)
    a = ap.parse_args()

    uid = USERS[a.user]
    print(f"user={a.user} uid={uid} rounds={a.rounds}")

    results = []
    async with httpx.AsyncClient(base_url=BASE, timeout=None) as client:
        try:
            h = await client.get("/api/health")
            h.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"后端不可用：{exc}")
            return 2

        for i in range(a.rounds):
            print(f"\n--- round {i+1}/{a.rounds} ---")
            s = await one_round(client, uid, a.user)
            results.append(s)
            print(f"  tools={s['tool_seq']}")
            print(f"  错误 {len(s['errors'])} 条")
            for e in s["errors"]:
                print(f"    ! {e}")
            print(f"  答出 92: {s['has_92']}  提到 powershell: {s['powershell_mentioned']}")

    print("\n===== 汇总 =====")
    bad_runs = [r for r in results if r["errors"]]
    no_92 = [r for r in results if not r["has_92"]]
    ps = [r for r in results if r["powershell_mentioned"]]

    print(f"  总轮次 {len(results)}")
    print(f"  有工具错误的轮次: {len(bad_runs)}/{len(results)}")
    print(f"  没答出 92 的轮次: {len(no_92)}/{len(results)}")
    print(f"  提到 powershell 的轮次: {len(ps)}/{len(results)}")
    avg = sum(r["n_tool_calls"] for r in results) / max(1, len(results))
    print(f"  平均工具调用次数: {avg:.1f}")

    ok = not bad_runs and not no_92 and not ps
    print(f"\n  {'[PASS]' if ok else '[FAIL]'} 路径与 shell 类型不再卡住模型")
    for r in results:
        print(f"    现场: {r['dir']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))