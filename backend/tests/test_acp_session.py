import asyncio
import json
import os
import sys
from pathlib import Path

from app.acp.config import load_acp_settings
from app.acp.session import GaSession

FIXTURE = str(Path(__file__).parent / "fixtures" / "fake_ga_bridge.py")


def run(coro):
    return asyncio.run(coro)


def settings(mode="chunks"):
    return load_acp_settings({
        "INNER_ACP_GA_COMMAND": json.dumps([sys.executable, FIXTURE]),
        "FAKE_GA_MODE": mode,
    })


def make(mode, events):
    env = {**os.environ, "FAKE_GA_MODE": mode}
    return GaSession("ga_test", "T", "task", settings=settings(mode),
                     cwd=str(Path.cwd()), env=env, publish=events.append)


def test_text_chunks_map_to_events_and_full_text():
    async def scenario():
        events = []
        s = make("chunks", events)
        await s.start()
        try:
            r = await s.prompt("hi", prompt_seq=1, timeout_s=10)
            return r, events, s
        finally:
            await s.close()

    r, events, s = run(scenario())
    assert r.status == "completed"
    assert r.full_text == "hello world"
    assert r.result == "hello world"
    assert [e["agent_id"] for e in events if e["type"] == "subagent_event"] == ["ga_test#1"] * 2
    assert [e["event"]["type"] for e in events if e["type"] == "subagent_event"] == [
        "text_delta", "text_delta"]


def test_tool_events_map_and_result_is_tail():
    async def scenario():
        events = []
        s = make("tools", events)
        await s.start()
        try:
            r = await s.prompt("hi", prompt_seq=1, timeout_s=10)
            return r, [e["event"] for e in events if e["type"] == "subagent_event"]
        finally:
            await s.close()

    r, evs = run(scenario())
    assert r.tool_calls == 1
    assert r.full_text == "reading...FINAL"
    assert r.result == "FINAL"
    assert [e["type"] for e in evs] == ["tool_start", "text_delta", "tool_result", "text_delta"]
    assert evs[0]["name"] == "file_read"
    assert evs[2]["is_error"] is False


def test_second_prompt_gets_new_card_id():
    async def scenario():
        events = []
        s = make("chunks", events)
        await s.start()
        try:
            await s.prompt("one", prompt_seq=1, timeout_s=10)
            await s.prompt("two", prompt_seq=2, timeout_s=10)
            return [e["agent_id"] for e in events if e["type"] == "subagent_start"]
        finally:
            await s.close()

    assert run(scenario()) == ["ga_test#1", "ga_test#2"]


def test_stop_marks_stopped():
    async def scenario():
        events = []
        s = make("hang", events)
        await s.start()
        try:
            task = asyncio.create_task(s.prompt("one", prompt_seq=1, timeout_s=30))
            await asyncio.sleep(0.2)
            await s.stop()
            r = await asyncio.wait_for(task, 10)
            return r.status, s.status
        finally:
            await s.close()

    assert run(scenario()) == ("stopped", "stopped")


def test_process_death_marks_errored():
    async def scenario():
        events = []
        s = make("crash", events)
        await s.start()
        try:
            r = await s.prompt("boom", prompt_seq=1, timeout_s=10)
            return r.status, s.status, s.snapshot()
        finally:
            await s.close()

    status, session_status, snap = run(scenario())
    assert status == "errored"
    assert session_status == "errored"
    assert snap["error"]


def make_env(mode, events, **extra):
    env = {**os.environ, "FAKE_GA_MODE": mode, **extra}
    return GaSession("ga_test", "T", "task", settings=settings(mode),
                     cwd=str(Path.cwd()), env=env, publish=events.append)


def test_protocol_error_marks_errored():
    async def scenario():
        events = []
        s = make("error", events)
        await s.start()
        try:
            r = await s.prompt("hi", prompt_seq=1, timeout_s=10)
            return r, s.status, s.snapshot(), events
        finally:
            await s.close()

    r, status, snap, events = run(scenario())
    assert r.status == "errored"
    assert status == "errored"
    assert "AcpError" in snap["error"]
    ends = [e for e in events if e["type"] == "subagent_end"]
    assert len(ends) == 1
    assert ends[0]["status"] == "errored"


def test_timeout_cancels_ga_and_marks_stopped(tmp_path):
    log = tmp_path / "ga_messages.log"

    async def scenario():
        events = []
        s = make_env("hang", events, FAKE_GA_LOG=str(log))
        await s.start()
        try:
            r = await s.prompt("hi", prompt_seq=1, timeout_s=0.3)
            return r, s.status, events
        finally:
            await s.close()

    r, status, events = run(scenario())
    assert r.status == "stopped"
    assert status == "stopped"
    assert r.stop_reason == "timeout"
    sent = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert any(m.get("method") == "session/cancel" for m in sent)
    ends = [e for e in events if e["type"] == "subagent_end"]
    assert len(ends) == 1
    assert ends[0]["status"] == "stopped"


def test_updates_after_terminal_are_dropped():
    async def scenario():
        events = []
        s = make("chunks", events)
        await s.start()
        try:
            await s.prompt("hi", prompt_seq=1, timeout_s=10)
            before = (len(events), s.last_seq, s.full_text, s.snapshot()["tool_calls"])
            s._on_update("ga_fake_1", {"sessionUpdate": "agent_message_chunk",
                                       "content": {"type": "text", "text": "late"}})
            s._on_update("ga_fake_1", {"sessionUpdate": "tool_call", "toolCallId": "t9",
                                       "title": "late_tool", "rawInput": {}})
            after = (len(events), s.last_seq, s.full_text, s.snapshot()["tool_calls"])
            return before, after
        finally:
            await s.close()

    before, after = run(scenario())
    assert before == after


def test_concurrent_prompt_is_rejected_without_new_card():
    async def scenario():
        events = []
        s = make("hang", events)
        await s.start()
        try:
            first = asyncio.create_task(s.prompt("one", prompt_seq=1, timeout_s=30))
            await asyncio.sleep(0.2)
            busy = None
            try:
                await s.prompt("two", prompt_seq=2, timeout_s=10)
            except Exception as exc:  # noqa: BLE001
                busy = type(exc).__name__
            await s.stop()
            await asyncio.wait_for(first, 10)
            return busy, [e["agent_id"] for e in events if e["type"] == "subagent_start"]
        finally:
            await s.close()

    busy, starts = run(scenario())
    assert busy == "AcpBusyError"
    assert starts == ["ga_test#1"]


def test_stop_then_pipe_close_stays_stopped(monkeypatch):
    async def scenario():
        events = []
        s = make("hang", events)
        await s.start()
        try:
            task = asyncio.create_task(s.prompt("one", prompt_seq=1, timeout_s=30))
            await asyncio.sleep(0.2)

            async def _no_cancel():
                return None

            # 模拟 Task 6 的「cancel 发出后 2s 仍未返回 → kill」：硬杀会让在途
            # prompt 以 AcpTransportClosed 失败，但用户主动停止不算故障。
            monkeypatch.setattr(s, "_cancel_quietly", _no_cancel)
            await s.stop()
            s.process_handle.kill()
            r = await asyncio.wait_for(task, 10)
            return r, s.status, s.snapshot()["error"], events
        finally:
            await s.close()

    r, status, error, events = run(scenario())
    assert r.status == "stopped"
    assert status == "stopped"
    assert error == ""
    ends = [e for e in events if e["type"] == "subagent_end"]
    assert len(ends) == 1
    assert ends[0]["status"] == "stopped"


def test_permission_request_emits_tool_pair():
    async def scenario():
        events = []
        s = make("permission", events)
        await s.start()
        try:
            r = await s.prompt("write a file", prompt_seq=1, timeout_s=10)
            return r, [e["event"] for e in events if e["type"] == "subagent_event"]
        finally:
            await s.close()

    r, evs = run(scenario())
    assert r.status == "completed"
    assert evs[0]["type"] == "tool_start"
    assert evs[0]["name"] == "permission:file_write"
    assert evs[1]["type"] == "tool_result"
    assert evs[1]["name"] == "permission:file_write"
    assert evs[1]["content"] == "allowed"
    assert evs[1]["is_error"] is False
