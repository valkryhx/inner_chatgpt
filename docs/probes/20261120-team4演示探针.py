"""Agent Team 演示：4 个子代理，2 并行查询 → 合并 → 写文档 → 画图。

## 这轮要验什么

1. **会不会用 file_patch（只发 diff）** —— 刚给 file_patch 加了四级降级匹配，
   要确认模型在真实任务里真的会挑它，而不是一律 file_write 整文件重写。
2. **会不会卡住** —— 用 sqlbotmcp（本地，0.0–0.4s 响应），排除 exa 的上游不稳，
   这样如果慢/卡，就是编排或生成侧的问题。

## 任务结构（自然表述，不点明「并行/串行」）

两个并行：分头查两类指标
两个串行：先写文档，再基于文档画图

## 判据

| 判据 | 能区分什么 |
| --- | --- |
| 子代理数 = 4 | 编排是否真的按 team 起 4 个 |
| `file_patch` 调用次数 > 0 | 模型是否真的用「只发 diff」 |
| 时间轴最大静默 | 卡住 vs 慢 |
| 产物落盘 | 任务是否真完成 |

## ⚠️ 上一版的缺陷已修

旧探针用 `team.get("runs") or []`，而 `/team` 返回里**没有 runs 字段**
（`routers/team.py` 只返回 team/tasks/events/messages），于是它对子代理
状态完全盲，字段缺失还静默降级成空列表。现在改为**直接从 DB 读**
`subagent_runs`，缺字段时直接暴露而不是假装正常。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("HLZ_PROBE_BASE", "http://127.0.0.1:18080").rstrip("/")
IDENT = os.environ.get("HLZ_PROBE_EMAIL", "hx")
PASSWORD = os.environ.get("HLZ_PROBE_PASS", "12345678")
TIMEOUT = int(os.environ.get("HLZ_PROBE_TIMEOUT", "1800"))
PG = os.environ.get("HLZ_PROBE_PG", "hlzsbxprobe-postgres-1")

PROMPT = """用 team 派 4 个 sub，基于 sqlbotmcp 演示一下：
先 2 个并行分头查宽带业务数据（一个查用户规模类指标，一个查渗透/占比类指标），
两个都查完以后再写一份文档汇总，最后基于文档出一张图。

要求：
- 查询用 sqlbotmcp，不要联网
- 文档要基于真实查到的数字，别编
- 图用 chart_spec 生成
- 最后告诉我落了哪些文件"""


def _req(path: str, body: dict | None = None, token: str | None = None,
         method: str | None = None, *, raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        BASE + path, data=data, method=method or ("POST" if data else "GET"),
        headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        if raw:
            return {"status": r.status}
        body_text = r.read().decode()
        return json.loads(body_text) if body_text.strip() else {}


def _db_runs(sid: str) -> list[dict]:
    """从 DB 读子代理状态。

    为什么不用 `/team` 的 runs：那里根本没有这个字段。缺字段时静默降级成
    空列表会让探针「看起来在工作」，实际对子代理完全盲（上一轮踩过）。
    """
    sql = (f"SELECT name||'|'||status||'|'||turns FROM subagent_runs "
           f"WHERE session_id='{sid}' ORDER BY name;")
    try:
        r = subprocess.run(
            ["docker", "exec", PG, "psql", "-U", "hlz", "-d", "hlaudz", "-tAc", sql],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[warn] 读 DB 失败：{e}")
        return []
    out = []
    for ln in (r.stdout or "").splitlines():
        ln = ln.strip()
        if not ln or "|" not in ln:
            continue
        name, status, turns = ln.split("|", 2)
        out.append({"name": name, "status": status,
                    "turns": int(turns) if turns.isdigit() else 0})
    return out


def _db_message(sid: str) -> tuple[str, int]:
    sql = (f"SELECT status||'|'||coalesce(length(content),0) FROM messages "
           f"WHERE session_id='{sid}' AND role='assistant' "
           f"ORDER BY created_at DESC LIMIT 1;")
    try:
        r = subprocess.run(
            ["docker", "exec", PG, "psql", "-U", "hlz", "-d", "hlaudz", "-tAc", sql],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30)
        ln = (r.stdout or "").strip()
        if "|" in ln:
            st, n = ln.split("|", 1)
            return st, int(n) if n.isdigit() else 0
    except Exception:
        pass
    return "?", 0


def main() -> int:
    auth = _req("/api/auth/login", {"identifier": IDENT, "password": PASSWORD})
    token = auth["access_token"]
    print(f"登录成功 user={auth['user']['username']}")

    sess = _req("/api/sessions", {"title": "team 4sub sqlbot 演示"}, token)
    sid = sess.get("id") or sess.get("session_id")
    print(f"会话 {sid}")

    _req(f"/api/sessions/{sid}/messages",
         {"text": PROMPT, "mode": "send"}, token, raw=True)
    print("已发送，开始轮询（每 15s）…\n")

    started = time.time()
    last = ""
    while time.time() - started < TIMEOUT:
        time.sleep(15)
        el = int(time.time() - started)
        try:
            team = _req(f"/api/sessions/{sid}/team", token=token)
        except urllib.error.HTTPError as e:
            print(f"[{el:4d}s] team 查询 {e.code}")
            continue
        tasks = team.get("tasks") or []
        runs = _db_runs(sid)
        st, nchars = _db_message(sid)

        bits = [f"msg={st}/{nchars}字"]
        for t in tasks:
            bits.append(f"{str(t.get('subject',''))[:10]}={t.get('status')}")
        for r in runs:
            bits.append(f"{r['name']}:{r['status']}/{r['turns']}轮")
        line = " | ".join(bits)
        if line != last:
            print(f"[{el:4d}s] {line}")
            last = line

        done_tasks = tasks and all(
            t.get("status") in ("completed", "cancelled", "failed") for t in tasks)
        done_runs = runs and all(
            r["status"] not in ("starting", "running") for r in runs)
        if done_tasks and done_runs:
            print(f"\n[{el:4d}s] 全部到终态")
            break
    else:
        print(f"\n超时 {TIMEOUT}s —— 仍未到终态")

    print("\n=== 子代理终态 ===")
    for r in _db_runs(sid):
        print(f"  {r['name']:<20s} {r['status']:<12s} {r['turns']} 轮")
    st, nchars = _db_message(sid)
    print(f"  assistant 消息：{st}，{nchars} 字")
    print(f"\nSESSION_ID={sid}")
    print(f"GEN_ID_TO_FIND=见下方导出命令")

    # 导出时间轴（供后续分析）
    print("\n导出时间轴：")
    print(f"  docker exec hlzsbxprobe-app-1 sh -c 'cat "
          f"/app/backend/data/logs/app-*.log' | grep agent-activity "
          f"| grep '<gen>' > work/_trace_team4.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())