"""真实 LLM 的多智能体沙箱 E2E：Agent Team / 并行子代理 / 双用户。

与构造测试的区别：这里跑**真实模型**，它会自己决定怎么写代码、
用哪个工具、写什么文件。构造测试只能证明「机制对了」，
这里验证的是「模型真的用起来时不会卡」。

判定仍以**工具输出与文件落点**为准，不看模型复述。

用法：
    python tests/manual/multiagent_llm_e2e.py --case team
    python tests/manual/multiagent_llm_e2e.py --case parallel
    python tests/manual/multiagent_llm_e2e.py --case users
    python tests/manual/multiagent_llm_e2e.py --case all
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
SANDBOX_ROOT = Path(r"D:\sandbox")


async def token_for(uid: str) -> str:
    from app import db
    from app.auth import create_access_token
    u = await db.get_user(uid)
    t, _ = create_access_token(uid, u.get("token_ver", 0))
    return t


def text_of(events: list[dict]) -> str:
    return "".join(e.get("text", "") for e in events if "text" in e)


def tool_msgs(events: list[dict]) -> list[str]:
    """所有工具调用的 name:content 串（判定用，比模型复述可靠）。"""
    out = []
    for e in events:
        if e.get("name") and "content" in e:
            c = e["content"]
            s = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
            out.append(f"{e['name']}: {s}")
    return out


async def run_case(client: httpx.AsyncClient, label: str, uid: str,
                   prompt: str, timeout_s: float = 420) -> dict:
    run_id = f"{int(time.time())}-{secrets.token_hex(2)}"
    out_dir = RUNS / f"mac-{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    token = await token_for(uid)
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}

    r = await client.post("/api/sessions", json={"title": f"{label}-{run_id}"},
                          headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    sid = r.json()["id"]

    events: list[dict] = []
    async with client.stream("POST", f"/api/sessions/{sid}/messages",
                             json={"text": prompt}, headers=headers,
                             timeout=timeout_s) as resp:
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

    (out_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")

    tools = [e.get("name") for e in events if e.get("name") and "input" in e]
    msgs = tool_msgs(events)
    errs = [m for m in msgs if "工具执行错误" in m or "can't open file" in m
            or "No such file" in m or "路径越界" in m]

    result = {
        "label": label, "uid": uid, "session": sid, "run_id": run_id,
        "tools": tools, "n_tools": len(tools),
        "errors": errs, "text_tail": text_of(events)[-300:],
        "tool_blob": "\n".join(msgs),
        "dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(
        json.dumps({k: v for k, v in result.items() if k != "tool_blob"},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# ---------------- 三个场景 ----------------

TEAM_PROMPT = """用 Agent Team 帮我做一件小事，验证多智能体能在沙箱里跑。

请：
1. 派 2 个 teammate（任意名字），让每个 teammate 各自用 code_run 跑一段 Python，
   在**自己的目录**里写一个文件（文件名带上自己的 agent 名），内容里包含
   `SENTINEL={sentinel}`，然后读回并复述内容。
2. 等两个都完成后，把它们的复述汇总告诉我。

要求：teammate 必须真的用 code_run 写文件，不要只回答问题。
"""

PARALLEL_PROMPT = """请**同时（并行）**派 3 个子代理，各自独立完成一件事：
每个子代理用 code_run 跑 Python，在自己目录里写一个 txt，
内容包含 `SENTINEL={sentinel}` 和它自己的编号，然后读回来确认。

三个都完成后告诉我：
- 每个子代理写出的文件内容是什么
- 它们是否互相看得到对方的文件（应该看不到）

注意：并行派发，不要一个一个来。
"""

USERS_PROMPT = """用 code_run 跑一段 Python：
1. 打印当前工作目录
2. 在自己 workspace 根下创建 `user_probe.txt`，写入 `USER={sentinel}`
3. 读回确认

然后把完整输出复述给我。
"""


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="all",
                    choices=["team", "parallel", "users", "all"])
    ap.add_argument("--timeout", type=float, default=420)
    a = ap.parse_args()

    which = ["team", "parallel", "users"] if a.case == "all" else [a.case]
    results: list[dict] = []

    async with httpx.AsyncClient(base_url=BASE, timeout=None) as client:
        try:
            h = await client.get("/api/health")
            h.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"后端不可用：{exc}")
            return 2

        if "team" in which:
            s = secrets.token_hex(4)
            print(f"\n===== Agent Team（2 teammate）sentinel={s} =====")
            r = await run_case(client, "team", USERS["hx"],
                               TEAM_PROMPT.format(sentinel=s), a.timeout)
            r["sentinel"] = s
            results.append(r)
            print(f"  tools({r['n_tools']}): {r['tools'][:12]}")

        if "parallel" in which:
            s = secrets.token_hex(4)
            print(f"\n===== 并行 3 子代理 sentinel={s} =====")
            r = await run_case(client, "parallel", USERS["hx"],
                               PARALLEL_PROMPT.format(sentinel=s), a.timeout)
            r["sentinel"] = s
            results.append(r)
            print(f"  tools({r['n_tools']}): {r['tools'][:12]}")

        if "users" in which:
            print(f"\n===== 两个用户各自执行 =====")
            sents = {}
            for name, uid in USERS.items():
                s = secrets.token_hex(4)
                sents[name] = s
                print(f"  [{name}] sentinel={s}")
                r = await run_case(client, f"users-{name}", uid,
                                   USERS_PROMPT.format(sentinel=s), a.timeout)
                r["sentinel"] = s
                results.append(r)
                print(f"    tools({r['n_tools']}): {r['tools'][:8]}")

    # ---------------- 判定 ----------------
    print("\n===== 判定（以工具输出与文件落点为准）=====")
    checks: list[tuple[str, bool, str]] = []

    for r in results:
        label = r["label"]
        checks.append((f"{label}: 无沙箱错误", not r["errors"],
                       "; ".join(r["errors"])[:120]))
        # LLM 侧故障要单独标注：它会让「没调工具」看起来像沙箱坏了，
        # 实际是上游连接池打满（PoolTimeout）。两者必须分开报。
        llm_bad = "连接中断" in r["text_tail"] or "PoolTimeout" in r["text_tail"]
        checks.append((f"{label}: LLM 侧正常（排除上游故障）", not llm_bad,
                       "上游 PoolTimeout/连接中断，与沙箱无关" if llm_bad else ""))
        checks.append((f"{label}: 模型真的调了工具", r["n_tools"] > 0,
                       f"{r['n_tools']} 次"))
        checks.append((f"{label}: 哨兵出现在工具输出里",
                       r["sentinel"] in r["tool_blob"], r["sentinel"]))
        # 容器内执行证据
        checks.append((f"{label}: 在容器内（/ws 或 IS_CONTAINER）",
                       "/ws" in r["tool_blob"] or "IS_CONTAINER" in r["tool_blob"], ""))

    # 多智能体场景：验证子代理目录真的被用到
    for r in results:
        if r["label"] in ("team", "parallel"):
            used = ".subagents" in r["tool_blob"]
            checks.append((f"{r['label']}: 子代理目录 .subagents 被使用", used, ""))

    bad = 0
    for name, ok, detail in checks:
        if not ok:
            bad += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    print(f"\n===== {len(checks)-bad}/{len(checks)} 通过 =====")
    for r in results:
        print(f"  现场: {r['dir']}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))