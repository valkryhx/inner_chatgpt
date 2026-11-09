import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from app.acp.config import load_acp_settings
from app.acp.manager import AcpLimitError, AcpSessionManager
from app.quota import Limits, QuotaExceeded, QuotaManager
from app.subagent.context import AgentContext


class RecordingBuffer:
    def __init__(self):
        self.events = []

    def put(self, ev):
        self.events.append(ev)

FIXTURE = str(Path(__file__).parent / "fixtures" / "fake_ga_bridge.py")


def run(coro):
    return asyncio.run(coro)


def make(**extra):
    env = {
        "INNER_ACP_GA_COMMAND": json.dumps([sys.executable, FIXTURE]),
        "INNER_ACP_PROMPT_TIMEOUT_S": "30",
        # 子进程环境走白名单；FAKE_GA_MODE 只有显式允许才会传给 fixture。
        "INNER_ACP_ENV_ALLOW": "FAKE_GA_MODE,FAKE_GA_LOG",
        **extra,
    }
    ctx = AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")
    buf = RecordingBuffer()
    return AcpSessionManager(ctx=ctx, buffer=buf, settings=load_acp_settings(env)), buf


def test_spawn_and_wait_completes(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr, _ = make()
        try:
            out = await mgr.spawn(task="say hi")
            await mgr.wait(out["agent_id"], timeout_s=10)
            return await mgr.result(out["agent_id"]), mgr.has_active()
        finally:
            await mgr.close_all()

    r, active = run(scenario())
    assert r["status"] == "completed"
    assert r["result"] == "hello world"
    assert active is False


def test_spawn_uses_user_workspace(tmp_path, monkeypatch):
    from app import workspace
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr, _ = make()
        try:
            out = await mgr.spawn(task="hi")
            return mgr._sessions[out["agent_id"]].cwd
        finally:
            await mgr.close_all()

    assert run(scenario()).startswith(str(tmp_path / "ws"))


def test_spawn_returns_immediately(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "hang")

    async def scenario():
        mgr, _ = make()
        try:
            t0 = time.monotonic()
            out = await mgr.spawn(task="long")
            elapsed = time.monotonic() - t0
            active = mgr.has_active()
            await mgr.stop(out["agent_id"])
            return elapsed, active
        finally:
            await mgr.close_all()

    elapsed, active = run(scenario())
    assert elapsed < 5
    assert active is True


def test_quota_rejects_second_prompt(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "hang")

    async def scenario():
        mgr, _ = make()
        mgr.quota = QuotaManager(Limits(user_acp_active=1, global_acp_active=2))
        try:
            await mgr.spawn(task="one")
            with pytest.raises(QuotaExceeded):
                await mgr.spawn(task="two")
        finally:
            await mgr.close_all()

    run(scenario())


def test_open_session_cap(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr, _ = make(INNER_ACP_MAX_OPEN_USER="1")
        try:
            await mgr.spawn(task="one")
            await asyncio.sleep(0.3)
            with pytest.raises(AcpLimitError):
                await mgr.spawn(task="two")
        finally:
            await mgr.close_all()

    run(scenario())


def test_stop_all_now_kills_process_synchronously(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "hang")

    async def scenario():
        mgr, _ = make()
        try:
            out = await mgr.spawn(task="long")
            handle = mgr._sessions[out["agent_id"]].process_handle
            mgr.stop_all_now()
            for _ in range(50):
                if handle.returncode is not None:
                    return True
                await asyncio.sleep(0.1)
            return False
        finally:
            await mgr.close_all()

    assert run(scenario()) is True


def test_crash_marks_errored(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "crash")

    async def scenario():
        mgr, _ = make()
        try:
            out = await mgr.spawn(task="boom")
            await mgr.wait(out["agent_id"], timeout_s=10)
            return await mgr.result(out["agent_id"])
        finally:
            await mgr.close_all()

    assert run(scenario())["status"] == "errored"


def test_send_creates_new_card(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr, buf = make()
        try:
            out = await mgr.spawn(task="one")
            aid = out["agent_id"]
            await mgr.wait(aid, timeout_s=10)
            await mgr.send(aid, "two")
            await mgr.wait(aid, timeout_s=10)
            return [e["agent_id"] for e in buf.events if e["type"] == "subagent_start"]
        finally:
            await mgr.close_all()

    cards = run(scenario())
    assert len(cards) == 2
    assert cards[0] == cards[1].replace("#2", "#1")
    assert cards[0].endswith("#1") and cards[1].endswith("#2")
