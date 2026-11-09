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
