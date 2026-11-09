import asyncio

from app.runtime import RuntimeHub


def run(coro):
    return asyncio.run(coro)


class FakeAcp:
    def __init__(self):
        self.stopped = 0
        self.closed = 0

    async def stop_generation(self):
        self.stopped += 1
        return 2

    async def close_all(self):
        self.closed += 1


def test_register_get_unregister():
    hub = RuntimeHub()
    m = FakeAcp()
    hub.register_acp_manager("u", "s", "g", m)
    assert hub.get_acp_manager("u", "s", "g") is m
    hub.unregister_acp_manager("u", "s", "g")
    assert hub.get_acp_manager("u", "s", "g") is None


def test_stop_acp_generation_delegates():
    async def scenario():
        hub = RuntimeHub()
        m = FakeAcp()
        hub.register_acp_manager("u", "s", "g", m)
        n = await hub.stop_acp_generation("u", "s", "g")
        return n, m.stopped

    assert run(scenario()) == (2, 1)


def test_stop_unknown_generation_is_noop():
    async def scenario():
        return await RuntimeHub().stop_acp_generation("u", "s", "g")

    assert run(scenario()) == 0


def test_shutdown_closes_acp_managers():
    async def scenario():
        hub = RuntimeHub()
        m = FakeAcp()
        hub.register_acp_manager("u", "s", "g", m)
        await hub.shutdown()
        return m.closed, hub.get_acp_manager("u", "s", "g")

    assert run(scenario()) == (1, None)
import pytest


def test_merged_events_waits_for_acp_extra():
    from app.routers.chat import _merged_events
    from app.subagent.events import EventBuffer

    class Extra:
        active = True

        def has_active(self):
            return self.active

        async def stop_all(self):
            self.active = False

    async def main_gen():
        yield {"type": "done", "status": "complete"}

    async def scenario():
        buf = EventBuffer()
        extra = Extra()

        async def child():
            await asyncio.sleep(0.05)
            buf.put({"type": "subagent_end", "agent_id": "ga_x#1",
                     "status": "completed", "turns": 0})
            extra.active = False

        task = asyncio.create_task(child())
        out = []
        async for ev in _merged_events(main_gen(), buf, object(), extra=extra):
            out.append(ev["type"])
        await task
        return out

    assert "subagent_end" in run(scenario())


def test_merged_events_stops_acp_extra_on_drain_timeout(monkeypatch):
    from app.routers.chat import _merged_events
    from app.subagent.events import EventBuffer

    monkeypatch.setenv("TEAM_DRAIN_TIMEOUT_S", "0.1")

    class Extra:
        def __init__(self):
            self.stops = 0

        def has_active(self):
            return True

        async def stop_all(self):
            self.stops += 1

    async def main_gen():
        yield {"type": "done", "status": "complete"}

    async def scenario():
        extra = Extra()
        async for _ in _merged_events(main_gen(), EventBuffer(), object(), extra=extra):
            pass
        return extra.stops

    assert run(scenario()) >= 1


def test_stop_endpoint_orders_graceful_acp_before_hard_kill(monkeypatch):
    from app import db
    from app.routers import chat
    from app.schemas import StopIn

    order = []

    async def fake_get_session(sid, uid):
        return {"id": sid, "user_id": uid}

    class FakeHub:
        async def stop_subagent_generation(self, sid, gid):
            order.append("subagents")
            return 1

        async def stop_acp_generation(self, uid, sid, gid):
            order.append("acp")
            return 1

    async def fake_stop_code_runs(uid, sid, gid):
        order.append("code_runs")

    monkeypatch.setattr(chat, "get_hub", lambda: FakeHub())
    monkeypatch.setattr(chat, "request_stop", lambda sid, gid: 1)
    monkeypatch.setattr(chat, "stop_code_runs", fake_stop_code_runs)
    monkeypatch.setattr(db, "get_session", fake_get_session)

    run(chat.stop_generation("s1", StopIn(generation_id="g1"), {"id": "u1"}))
    assert order == ["subagents", "acp", "code_runs"]


def test_cleanup_calls_acp_stop_all_now_before_awaits():
    from app.routers import chat

    calls = []

    class Acp:
        def stop_all_now(self):
            calls.append("acp_sync")

        async def close_all(self):
            calls.append("acp_close")

    class Host:
        def stop_all_now(self):
            calls.append("host_sync")

        async def close_all(self):
            calls.append("host_close")

    class Team:
        async def events_since(self, seq):
            return []

        async def set_status(self, status):
            return None

    class Turn:
        host = Host()
        team = None
        acp = Acp()

    class Hub:
        def unregister_subagent_host(self, *a):
            calls.append("unregister")

        def unregister_acp_manager(self, *a):
            calls.append("acp_unregister")

    async def scenario():
        await chat._cleanup_turn(Turn(), Hub(), "u", "s", "g", "", "stopped")

    run(scenario())
    assert calls.index("acp_sync") < calls.index("acp_close")
    assert calls.index("acp_sync") < calls.index("host_close")
    # Controller addition: cleanup must also drop the ACP manager registration.
    assert calls.count("acp_unregister") == 1
    assert calls.index("acp_close") < calls.index("acp_unregister")
