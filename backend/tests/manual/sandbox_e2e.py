"""第 9 步：真实用户（hx / xh）的沙箱 E2E。

与 ``tests/e2e`` 的区别：
- 不新建 e2e 用户，而是**直接给 hx / xh 签 token**（它们才在沙箱白名单里）；
- 判定不只看「工具被调用过」，还要看**结果落在哪个宿主目录**、
  **另一个用户的目录有没有被污染**（这是沙箱的核心价值）；
- 同一 harness 跑一个「白名单外用户」的负对照 —— 它必须走进程内、不产生容器。

前置：
1. 后端在跑（``python run.py``），且环境里 ``HLZ_SANDBOX_MODE`` 非 off；
2. 有可用 LLM 配置。

用法：
    python tests/manual/sandbox_e2e.py            # 两个真实用户 + 负对照
    python tests/manual/sandbox_e2e.py --mode strict
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8000")
RUNS = Path(__file__).resolve().parents[1] / "e2e" / "sandbox_runs"

# 白名单里的两个真实用户
REAL_USERS = {"hx": "u_c56b8337dd09", "xh": "u_343a46896bff"}
# 负对照：不在白名单里
CONTROL_USER = {"e2e_ctl": None}  # 运行时注册

SANDBOX_ROOT = Path(os.environ.get("HLZ_SANDBOX_WORKSPACES_DIR") or r"D:\sandbox")


async def token_for(user_id: str) -> str:
    """直接签一个 access token（绕过密码，只用于验证）。"""
    from app import db
    from app.auth import create_access_token
    u = await db.get_user(user_id)
    if not u:
        raise SystemExit(f"用户不存在: {user_id}")
    token, _ = create_access_token(user_id, u.get("token_ver", 0))
    return token


async def new_session(client: httpx.AsyncClient, token: str, title: str) -> str:
    r = await client.post("/api/sessions", json={"title": title},
                          headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()["id"]


async def send(client: httpx.AsyncClient, token: str, sid: str, text: str,
               timeout_s: float) -> tuple[list[dict], str]:
    """发一条消息，逐行收 SSE，返回 (事件列表, 累积文本)。"""
    events: list[dict] = []
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    started = time.time()
    async with client.stream("POST", f"/api/sessions/{sid}/messages",
                             json={"text": text}, headers=headers,
                             timeout=timeout_s) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if time.time() - started > timeout_s:
                events.append({"type": "_timeout"})
                break
            if not line.startswith("data: "):
                continue
            raw = line[6:].strip()
            if raw in ("", "[DONE]"):
                if raw == "[DONE]":
                    break
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            events.append(ev)
    return events, text_of(events)


def text_of(events: list[dict]) -> str:
    """累积文本。

    Hlaudz 的 SSE 是**裸事件体**，不带 ``type`` 字段：
    - 增量文本 = ``{"text": "..."}``
    - 工具调用 = ``{"id","name","input"}``
    - 工具结果 = ``{"id","name","content","is_error"}``
    - 控制类（team/model/message_id）才带 ``type`` 或其它字段。
    第一版按 ``type=="text_delta"`` 解析，结果全空（假阴性），记在 §11 实施日志里。
    """
    return "".join(e.get("text", "") for e in events if "text" in e)


def tool_names(events: list[dict]) -> list[str]:
    return [e["name"] for e in events if e.get("name") and "input" in e]


def tool_results(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("name") and "content" in e]


async def run_case(client: httpx.AsyncClient, label: str, user_id: str | None,
                   prompt: str, timeout_s: float, *, register: bool = False) -> dict:
    """跑一个用户的一次会话，落盘并返回摘要。"""
    run_id = f"{int(time.time())}-{secrets.token_hex(2)}"
    out_dir = RUNS / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    if register:
        # 负对照需要新用户：走正常注册
        uname = f"e2e_ctl_{run_id}".replace("-", "_")
        r = await client.post("/api/auth/register",
                              json={"email": f"{uname}@e2e.local", "username": uname,
                                    "password": "e2e-pass-123"})
        r.raise_for_status()
        token = r.json()["access_token"]
        me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        uid = me.json().get("id") if me.status_code == 200 else "unknown"
    else:
        uid = user_id
        token = await token_for(user_id)

    sid = await new_session(client, token, f"{label}-{run_id}")
    events, text = await send(client, token, sid, prompt, timeout_s)

    result = {
        "run_id": run_id,
        "label": label,
        "user_id": uid,
        "session_id": sid,
        "elapsed_s": 0,
        "tools": tool_names(events),
        "text": text,
        "n_events": len(events),
        "dir": str(out_dir),
    }
    (out_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


PROMPT = (
    "请用 code_run 工具（code_type=python）执行下面这段代码，"
    "然后把它的**完整输出**原样告诉我：\n"
    "import os, socket\n"
    "print('SENTINEL={sentinel}')\n"
    "print('CWD', os.getcwd())\n"
    "print('IS_CONTAINER', os.path.exists('/.dockerenv'))\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 443), timeout=3); print('NET open')\n"
    "except Exception as e:\n"
    "    print('NET blocked', type(e).__name__)\n"
    "\n另外再用 file_write 在 {fname} 写入 SENTINEL={sentinel}。"
)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default=os.environ.get("HLZ_SANDBOX_MODE", ""),
                    help="仅用于打印，实际以后端环境变量为准")
    ap.add_argument("--timeout", type=float, default=240.0)
    args = ap.parse_args()

    print(f"BASE={BASE}  backend-mode={args.mode or '(env)'}")
    print(f"SANDBOX_ROOT={SANDBOX_ROOT}")

    results: list[dict] = []
    sentinels: dict[str, str] = {}

    async with httpx.AsyncClient(base_url=BASE, timeout=None) as client:
        # 健康检查
        try:
            h = await client.get("/api/health")
            h.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"后端不可用：{exc}\n请先 python run.py")
            return 2

        # ---- 两个真实用户 ----
        fnames: dict[str, str] = {}
        for name, uid in REAL_USERS.items():
            sentinel = secrets.token_hex(4)
            sentinels[name] = sentinel
            # 每轮唯一文件名：复用同名会让「写前必读」门禁拦住覆盖写，
            # 落盘断言就会读到上一轮的旧内容（第一版踩过）。
            fname = f"probe_{secrets.token_hex(3)}.txt"
            fnames[name] = fname
            print(f"\n== {name} ({uid}) sentinel={sentinel} file={fname} ==")
            r = await run_case(client, name, uid,
                               PROMPT.format(sentinel=sentinel, fname=fname), args.timeout)
            results.append(r)
            print(f"  tools={r['tools']}  events={r['n_events']}")
            print(f"  输出前 200 字：{r['text'][:200]!r}")

        # ---- 负对照：白名单外用户 ----
        sentinel = secrets.token_hex(4)
        sentinels["control"] = sentinel
        print(f"\n== control（白名单外新用户）sentinel={sentinel} ==")
        r = await run_case(client, "control", None,
                           PROMPT.format(sentinel=sentinel, fname="ctl_probe.txt"),
                           args.timeout, register=True)
        results.append(r)
        print(f"  tools={r['tools']}  events={r['n_events']}")
        print(f"  输出前 200 字：{r['text'][:200]!r}")

    # ---- 判定 ----
    print("\n== 判定 ==")
    checks: list[tuple[str, bool, str]] = []

    for name, uid in REAL_USERS.items():
        r = next(x for x in results if x["label"] == name)
        s = sentinels[name]
        checks.append((f"{name}: 结果里复述了哨兵", s in r["text"], s))
        checks.append((f"{name}: 输出显示在容器内", "IS_CONTAINER True" in r["text"],
                       r["text"][:80].replace("\n", " ")))
        checks.append((f"{name}: 网络被阻断", "NET blocked" in r["text"], ""))
        checks.append((f"{name}: 工作目录是 /ws", "CWD /ws" in r["text"], ""))
        # 宿主侧落盘：每轮唯一文件名
        f = SANDBOX_ROOT / uid / fnames[name]
        content = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
        checks.append((f"{name}: 落盘到 D:\\sandbox\\{uid}",
                       content.strip() == f"SENTINEL={s}", str(f)))

    # 交叉污染检查：两个用户各自的文件里只能有自己的哨兵
    hx_file = SANDBOX_ROOT / REAL_USERS["hx"] / fnames["hx"]
    xh_file = SANDBOX_ROOT / REAL_USERS["xh"] / fnames["xh"]
    if hx_file.exists() and xh_file.exists():
        hx_text = hx_file.read_text(encoding="utf-8", errors="replace")
        xh_text = xh_file.read_text(encoding="utf-8", errors="replace")
        no_cross = (sentinels["hx"] in hx_text and sentinels["xh"] not in hx_text
                    and sentinels["xh"] in xh_text and sentinels["hx"] not in xh_text)
        checks.append(("两用户互不串写", no_cross, f"hx={hx_text.strip()} xh={xh_text.strip()}"))

    # 负对照：不该有容器、不该在 D:\sandbox
    ctl = next(x for x in results if x["label"] == "control")
    d_sandbox_dirs = {p.name for p in SANDBOX_ROOT.iterdir() if p.is_dir()} \
        if SANDBOX_ROOT.exists() else set()
    checks.append(("负对照: 不在 D:\\sandbox 建目录", ctl["user_id"] not in d_sandbox_dirs,
                   ctl["user_id"]))

    bad = 0
    for name, ok, detail in checks:
        flag = "PASS" if ok else "FAIL"
        if not ok:
            bad += 1
        print(f"  [{flag}] {name}" + (f" — {detail}" if detail else ""))

    print(f"\n== {len(checks)-bad}/{len(checks)} 通过 ==")
    print("现场：" + ", ".join(r["dir"] for r in results))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))