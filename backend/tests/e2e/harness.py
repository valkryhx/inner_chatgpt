"""Hlaudz 真实 e2e harness：真 LLM + 真工具 + 事件落盘。

契约见 .claude/skills/hlaudz-e2e/SKILL.md §3。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

BASE = os.environ.get("HLZ_E2E_BASE", "http://127.0.0.1:8000")
RUNS = Path(__file__).resolve().parent / "runs"
DB = Path(__file__).resolve().parents[2] / "data" / "app.db"

DEFAULT_LLM = {
    "label": "湛卢-e2e",
    "wire_api": "openai_chat",
    "base_url": "https://ecloud.10086.cn/api/query/aigateway/v1",
    "model": "moma/deepseek-latest-flash",
    "context_window": 400000,
    "max_tokens": 8192,
    "is_default": True,
}


def zhanlu_api_key() -> str:
    """从已有配置复制湛卢的 key（不打印、不落盘）。"""
    con = sqlite3.connect(str(DB))
    try:
        row = con.execute(
            "select api_key from user_llm_configs "
            "where base_url like %s and api_key != '' limit 1" % "'%ecloud.10086.cn%'"
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        raise RuntimeError("找不到湛卢 API key（user_llm_configs 里没有 ecloud.10086.cn 的配置）")
    return row[0]


async def ensure_user(client: httpx.AsyncClient, username: str) -> str:
    email = f"{username}@e2e.local"
    r = await client.post("/api/auth/register",
                          json={"email": email, "username": username,
                                "password": "e2e-pass-123"})
    if r.status_code >= 400:
        r = await client.post("/api/auth/login",
                              json={"identifier": email, "password": "e2e-pass-123"})
    r.raise_for_status()
    return r.json()["access_token"]


async def ensure_zhanlu(client: httpx.AsyncClient, token: str) -> str:
    headers = {"Authorization": f"Bearer {token}"}
    existing = await client.get("/api/me/llm-configs", headers=headers)
    existing.raise_for_status()
    for cfg in existing.json():
        if "ecloud.10086.cn" in (cfg.get("base_url") or ""):
            return cfg["id"]
    body = dict(DEFAULT_LLM)
    body["api_key"] = zhanlu_api_key()
    r = await client.post("/api/me/llm-configs", json=body, headers=headers)
    r.raise_for_status()
    return r.json()["id"]


async def new_session(client: httpx.AsyncClient, token: str, title: str) -> str:
    r = await client.post("/api/sessions", json={"title": title},
                          headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()["id"]


async def send_message(client, token, sid, text, timeout_s):
    """读 SSE：event: <type> + data: {...}；把 type 合并进事件对象。"""
    events: list[dict[str, Any]] = []
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    body = {"text": text}
    cur_type = ""
    async with client.stream("POST", f"/api/sessions/{sid}/messages",
                             json=body, headers=headers, timeout=timeout_s) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("event:"):
                cur_type = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                events.append({"type": "_unparsed", "raw": payload[:200]})
                continue
            if isinstance(data, dict):
                if not data.get("type"):
                    data["type"] = cur_type
                events.append(data)
    return events


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    tools = [e.get("name") for e in events if e.get("type") == "tool_start"]
    texts = "".join(e.get("text", "") for e in events if e.get("type") in ("text_delta", "message_delta"))
    errors = [e.get("error") for e in events if e.get("type") == "error"]
    done = next((e for e in reversed(events) if e.get("type") == "done"), {})
    tasks: dict[str, str] = {}
    for e in events:
        if e.get("type") not in ("task_created", "task_updated"):
            continue
        payload = e.get("payload") or {}
        task = payload.get("task") or payload
        tid = task.get("task_id")
        if tid and task.get("status"):
            tasks[tid] = task["status"]
    return {"tools": tools, "text": texts, "errors": errors, "done": done, "tasks": tasks}


def evaluate(case: dict[str, Any], summary: dict[str, Any]) -> list[dict[str, Any]]:
    tools = summary["tools"]
    text = summary["text"]
    checks: list[dict[str, Any]] = []
    for name in case.get("expect_tools", []):
        checks.append({"kind": "expect_tool", "name": name, "ok": name in tools})
    for name in case.get("forbid_tools", []):
        checks.append({"kind": "forbid_tool", "name": name, "ok": name not in tools})
    for name, count in (case.get("tool_min_counts") or {}).items():
        checks.append({"kind": "tool_min_count", "name": f"{name}>={count}",
                       "ok": tools.count(name) >= count})
    for name, count in (case.get("tool_counts") or {}).items():
        checks.append({"kind": "tool_count", "name": f"{name}={count}",
                       "ok": tools.count(name) == count})
    if case.get("expect_all_tasks_completed"):
        states = summary.get("tasks") or {}
        want = int(case.get("min_tasks", 1))
        ok = len(states) >= want and all(v == "completed" for v in states.values())
        checks.append({"kind": "tasks_completed", "name": f"{len(states)}/{want}",
                       "ok": ok})
    sentinel = case.get("sentinel")
    if sentinel:
        checks.append({"kind": "sentinel", "name": sentinel, "ok": sentinel in text})
    if case.get("expect_done", True):
        status = (summary.get("done") or {}).get("status")
        checks.append({"kind": "done", "name": status, "ok": status == "complete"})
    return checks


async def run_case(case: dict[str, Any]) -> dict[str, Any]:
    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"
    out_dir = RUNS / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    async with httpx.AsyncClient(base_url=BASE, timeout=None) as client:
        token = await ensure_user(client, f"e2e_{run_id}".replace("-", "_"))
        await ensure_zhanlu(client, token)
        sid = await new_session(client, token, case.get("name", run_id))
        try:
            events = await send_message(client, token, sid, case["prompt"],
                                        case.get("timeout_s", 300))
        except Exception as exc:  # noqa: BLE001 - 现场必须落盘
            events = [{"type": "_transport_error", "error": f"{type(exc).__name__}: {exc}"}]
        try:
            msgs = (await client.get(f"/api/sessions/{sid}/messages",
                                     headers={"Authorization": f"Bearer {token}"})).json()
        except Exception:  # noqa: BLE001
            msgs = []

    summary = summarize(events)
    checks = evaluate(case, summary)
    result = {
        "run_id": run_id,
        "case": case.get("name", run_id),
        "session_id": sid,
        "elapsed_s": round(time.time() - started, 1),
        "tools": summary["tools"],
        "errors": summary["errors"],
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
        "dir": str(out_dir),
    }
    (out_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "messages.json").write_text(
        json.dumps(msgs, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
