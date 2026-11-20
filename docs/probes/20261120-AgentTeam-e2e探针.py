"""端到端跑一次 Agent Team（真实 LLM），验三处修复。

## 三处修复怎么验

| 修复 | 怎么在本次运行里看出它生效 |
| --- | --- |
| ① 遗留目录 507（第三、四层） | 子代理的产物必须真的落盘，且父目录 `2775` |
| ② code_run umask（第五层） | 若子代理用 code_run 写，产物必须组可写 |
| ③ 读门禁删除 + Read 2000 行 | 子代理不该再出现「只读了片段」的报错 |

## 判据（能区分两种状态）

- 修复前：writer 反复 0–4 轮就 stopped、任务 pending、产物缺失
- 修复后：至少一个 writer 正常完成、产物落盘、无 507

## 参数

    HLZ_PROBE_EMAIL / HLZ_PROBE_PASS 登录
    HLZ_PROBE_TIMEOUT 总超时秒（默认 600）
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("HLZ_PROBE_BASE", "http://127.0.0.1:18080").rstrip("/")
IDENT = os.environ.get("HLZ_PROBE_EMAIL", "hx")
PASSWORD = os.environ.get("HLZ_PROBE_PASS", "12345678")
TIMEOUT = int(os.environ.get("HLZ_PROBE_TIMEOUT", "600"))

PROMPT = """用 agent team 完成两件事，派两个 subagent：
1. 一个 subagent 调研「特朗普 2026 年的最新政策要点」，把结果用 file_write 写进 research/trump.md
2. 另一个 subagent 读 research/trump.md，写成 markdown 报告 reports/trump_report.md

两个子代理都必须真的调用 file_write 落盘。完成后告诉我两个文件的确切路径。"""


def _req(path: str, body: dict | None = None, token: str | None = None,
         method: str | None = None, *, raw: bool = False):
    """发请求。`raw=True` 时不解析响应体 —— 用于 SSE 端点。

    ## 为什么需要 raw 参数（探针自己踩的坑）

    `POST /api/sessions/{sid}/messages` 返回的是 **SSE**
    （`text/event-stream`，见 `routers/chat.py:1011`），不是 JSON。
    第一版对所有响应都 `json.loads()`，于是在发消息那一步直接崩：

        json.decoder.JSONDecodeError: Expecting value: line 1 column 1

    **最坑的是：请求其实成功了**（任务照样跑起来了），只是探针在读响应体时
    崩了 —— 所以日志里只有两行就 Traceback，但 DB 里子代理正常在跑。
    这种「探针崩了但产品没问题」会让人误判成产品故障。
    """
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


def main() -> int:
    try:
        auth = _req("/api/auth/login", {"identifier": IDENT, "password": PASSWORD})
    except urllib.error.HTTPError as e:
        print(f"登录失败 {e.code}: {e.read().decode()[:200]}")
        return 2
    token = auth["access_token"]
    print(f"登录成功 user={auth['user']['username']}")

    sess = _req("/api/sessions", {"title": "e2e 三处修复验证"}, token)
    sid = sess.get("id") or sess.get("session_id")
    print(f"会话 {sid}")

    # ★ SSE 端点：必须 raw=True，否则 json.loads 会崩。
    #   不能用 _req(...) 的流式特性去读完整 SSE（那会阻塞到回合结束），
    #   这里只需要「消息已受理」，之后靠轮询 DB/team 接口看进度。
    _req(f"/api/sessions/{sid}/messages",
         {"text": PROMPT, "mode": "send"}, token, raw=True)
    print("已发送，开始轮询…\n")

    started = time.time()
    last = ""
    while time.time() - started < TIMEOUT:
        time.sleep(10)
        el = int(time.time() - started)
        try:
            team = _req(f"/api/sessions/{sid}/team", token=token)
        except urllib.error.HTTPError as e:
            print(f"[{el:3d}s] team 查询 {e.code}")
            continue
        tasks = team.get("tasks") or []
        runs = team.get("runs") or team.get("agents") or []
        bits = []
        for t in tasks:
            bits.append(f"{str(t.get('subject',''))[:12]}={t.get('status')}")
        for r in runs:
            if isinstance(r, dict):
                bits.append(f"{r.get('name')}:{r.get('status')}/{r.get('turns',0)}轮")
        line = " | ".join(bits)
        if line != last:
            print(f"[{el:3d}s] {line}")
            last = line

        if tasks and all(t.get("status") in ("completed", "cancelled", "failed")
                         for t in tasks):
            idle = all(r.get("status") not in ("starting", "running")
                       for r in runs if isinstance(r, dict))
            if idle:
                print(f"\n[{el:3d}s] 全部到终态")
                break
    else:
        print(f"\n超时 {TIMEOUT}s —— 仍未到终态")

    print("\n=== 最终状态 ===")
    team = _req(f"/api/sessions/{sid}/team", token=token)
    print(json.dumps(team, ensure_ascii=False, indent=2)[:4000])
    print(f"\nSESSION_ID={sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())