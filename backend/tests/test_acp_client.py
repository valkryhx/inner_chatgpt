import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from app.acp.client import AcpBusyError, AcpClient, AcpError
from app.acp.transport import AcpProcess, AcpTransportClosed

FIXTURE = str(Path(__file__).parent / "fixtures" / "fake_ga_bridge.py")


def run(coro):
    return asyncio.run(coro)


async def make(mode="chunks", permission="allow", **env):
    p = AcpProcess([sys.executable, FIXTURE], cwd=str(Path.cwd()),
                   env={**os.environ, "FAKE_GA_MODE": mode, **env})
    p.start()
    updates = []
    c = AcpClient(p, permission=permission, on_update=lambda sid, u: updates.append(u))
    await c.start()
    return p, c, updates


def test_start_returns_agent_info():
    async def scenario():
        p, c, _ = await make()
        try:
            return c.agent_info["name"]
        finally:
            await c.close()

    assert run(scenario()) == "fake-ga"


def test_update_notifications_are_dispatched():
    async def scenario():
        p, c, updates = await make("chunks")
        try:
            s = await c.new_session(str(Path.cwd()))
            await c.prompt(s["sessionId"], "hi", timeout_s=10)
            return [u["content"]["text"] for u in updates]
        finally:
            await c.close()

    assert run(scenario()) == ["hello ", "world"]


def test_permission_request_is_auto_answered():
    async def scenario():
        p, c, updates = await make("permission", permission="allow")
        try:
            s = await c.new_session(str(Path.cwd()))
            r = await c.prompt(s["sessionId"], "write a file", timeout_s=10)
            return r["stopReason"], c.permission_requests, updates
        finally:
            await c.close()

    reason, requests, updates = run(scenario())
    assert reason == "end_turn"
    assert len(requests) == 1
    assert updates[-1]["content"]["text"] == "answered"


def test_permission_deny_is_sent(tmp_path):
    log = tmp_path / "msgs.jsonl"

    async def scenario():
        p, c, _ = await make("permission", permission="deny", FAKE_GA_LOG=str(log))
        try:
            s = await c.new_session(str(Path.cwd()))
            await c.prompt(s["sessionId"], "write a file", timeout_s=10)
        finally:
            await c.close()

    run(scenario())
    sent = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    answers = [m for m in sent if m.get("id") == "perm_fake" and m.get("result")]
    assert answers and answers[0]["result"]["outcome"]["optionId"] == "reject_once"


def test_second_prompt_is_rejected_locally():
    async def scenario():
        p, c, _ = await make("hang")
        try:
            s = await c.new_session(str(Path.cwd()))
            first = asyncio.create_task(c.prompt(s["sessionId"], "one", timeout_s=30))
            await asyncio.sleep(0.2)
            with pytest.raises(AcpBusyError):
                await c.prompt(s["sessionId"], "two", timeout_s=5)
            await c.cancel(s["sessionId"])
            return await first
        finally:
            await c.close()

    assert run(scenario())["stopReason"] == "cancelled"


def test_prompt_timeout_clears_busy_and_pending():
    async def scenario():
        p, c, _ = await make("hang")
        try:
            s = await c.new_session(str(Path.cwd()))
            with pytest.raises(asyncio.TimeoutError):
                await c.prompt(s["sessionId"], "slow", timeout_s=0.3)
            cleared = (c._active_prompt is None, c._pending == {})
            # 第二次必须再次超时而不是 AcpBusyError：只有 finally 真的清了标记才会这样。
            with pytest.raises(asyncio.TimeoutError):
                await c.prompt(s["sessionId"], "slow again", timeout_s=0.3)
            return cleared
        finally:
            await c.close()

    assert run(scenario()) == (True, True)


def test_prompt_fails_when_process_dies():
    async def scenario():
        p, c, _ = await make("crash")
        try:
            s = await c.new_session(str(Path.cwd()))
            with pytest.raises(AcpTransportClosed):
                await c.prompt(s["sessionId"], "boom", timeout_s=10)
        finally:
            await c.close()

    run(scenario())


def test_jsonrpc_error_is_raised():
    async def scenario():
        p, c, _ = await make("error")
        try:
            s = await c.new_session(str(Path.cwd()))
            with pytest.raises(AcpError) as exc:
                await c.prompt(s["sessionId"], "x", timeout_s=10)
            return exc.value.code
        finally:
            await c.close()

    assert run(scenario()) == -32603


def test_unknown_request_method_gets_method_not_found():
    async def scenario():
        p, c, _ = await make()
        sent = []

        async def fake_send(msg):
            sent.append(msg)

        c._process.send = fake_send
        try:
            c._dispatch({"jsonrpc": "2.0", "id": "u1", "method": "no/such", "params": {}})
            await asyncio.sleep(0.05)
            return sent[-1]
        finally:
            await c.close()

    reply = run(scenario())
    assert reply["id"] == "u1"
    assert reply["error"]["code"] == -32601
