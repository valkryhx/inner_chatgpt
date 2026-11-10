"""跨回合通知 e2e：第一轮派子代理完成后结束回合，第二轮应看到 [任务通知]。

用法：python -m tests.e2e.cross_turn
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from pathlib import Path

import httpx

from .harness import BASE, RUNS, ensure_user, ensure_zhanlu, new_session, send_message

SENTINEL = "SENTINEL=" + secrets.token_hex(3)


async def main() -> int:
    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:4]}-cross"
    out = RUNS / run_id
    out.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(base_url=BASE, timeout=None) as client:
        token = await ensure_user(client, f"e2e_{run_id}".replace("-", "_"))
        await ensure_zhanlu(client, token)
        sid = await new_session(client, token, "cross-turn-notify")

        first = ("派一个子代理计算 6*7，并要求它在结果里原样包含 "
                 f"{SENTINEL}。派完后不要等它，直接结束这一轮，告诉我已经派出了。")
        ev1 = await send_message(client, token, sid, first, 300)

        second = "刚才那个子代理完成了吗？如果已经完成，直接把它的结果复述给我，不要再派新的。"
        ev2 = await send_message(client, token, sid, second, 300)

    tools = [e.get("name") for e in ev2 if e.get("type") == "tool_start"]
    text2 = "".join(e.get("text", "") for e in ev2 if e.get("type") in ("text_delta", "message_delta"))
    checks = {
        "first_spawned": any(e.get("name") == "spawn_agent"
                             for e in ev1 if e.get("type") == "tool_start"),
        "no_second_spawn": "spawn_agent" not in tools,
        "sentinel_visible": SENTINEL in text2 or SENTINEL in json.dumps(ev2, ensure_ascii=False),
    }
    result = {"run_id": run_id, "checks": checks, "ok": all(checks.values()),
              "tools_turn2": tools, "dir": str(out)}
    (out / "events_turn1.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in ev1), encoding="utf-8")
    (out / "events_turn2.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in ev2), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
