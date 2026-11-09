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
    """R15: the graceful ACP stop must precede the hard kill.

    Uses a real RuntimeHub with a registered fake manager, so a missing
    registration (or a reordered endpoint) actually fails here instead of
    being faked away by the double.
    """
    from app import db
    from app.routers import chat
    from app.runtime import RuntimeHub
    from app.schemas import StopIn

    order = []

    async def fake_get_session(sid, uid):
        return {"id": sid, "user_id": uid}

    class FakeAcp:
        async def stop_generation(self):
            order.append("acp")
            return 1

    class FakeHost:
        def list_agents(self):
            return []

        async def stop_all(self):
            order.append("subagents")

    def fake_request_stop(sid, gid):
        order.append("request_stop")
        return 1

    def fake_cancel_generation(uid, sid, gid):
        order.append("cancel")
        return 1

    async def fake_stop_code_runs(uid, sid, gid):
        order.append("code_runs")

    hub = RuntimeHub()
    hub.register_acp_manager("u1", "s1", "g1", FakeAcp())
    hub.register_subagent_host("u1", "s1", "g1", "", FakeHost())

    monkeypatch.setattr(chat, "get_hub", lambda: hub)
    monkeypatch.setattr(chat, "request_stop", fake_request_stop)
    monkeypatch.setattr(chat, "cancel_generation", fake_cancel_generation)
    monkeypatch.setattr(chat, "stop_code_runs", fake_stop_code_runs)
    monkeypatch.setattr(db, "get_session", fake_get_session)

    run(chat.stop_generation("s1", StopIn(generation_id="g1"), {"id": "u1"}))
    assert order == ["request_stop", "acp", "cancel", "subagents", "code_runs"]


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
    # R15: the registration is dropped in the cancellation-proof synchronous
    # section, right after acp.stop_all_now(), so no await can skip it.
    assert calls.count("acp_unregister") == 1
    assert calls.index("acp_sync") < calls.index("acp_unregister")
    assert calls.index("acp_unregister") < calls.index("host_close")

# ---------- R15: registration / graceful-first stop / leak-proof cleanup ----------
def test_acp_manager_registration_helper_wires_the_hub():
    from app.routers import chat
    from app.runtime import RuntimeHub

    class FakeAcp:
        pass

    class Turn:
        acp = FakeAcp()

    hub = RuntimeHub()
    chat._register_acp_manager(hub, "u", "s", "g", Turn())
    assert hub.get_acp_manager("u", "s", "g") is Turn.acp

    class NoAcpTurn:
        acp = None

    chat._register_acp_manager(hub, "u", "s", "g2", NoAcpTurn())
    assert hub.get_acp_manager("u", "s", "g2") is None


def test_cleanup_unregisters_acp_manager_even_if_host_close_fails(monkeypatch):
    """A failing host.close_all() must not leak the hub's ACP registration."""
    from app.routers import chat
    from app.runtime import RuntimeHub

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(chat.team_store, "release_generation", _noop)

    class Acp:
        def stop_all_now(self):
            pass

        async def close_all(self):
            pass

    class Host:
        def stop_all_now(self):
            pass

        async def close_all(self):
            raise RuntimeError("host close boom")

    class Turn:
        host = Host()
        team = None
        acp = Acp()

    hub = RuntimeHub()
    hub.register_acp_manager("u", "s", "g", Turn.acp)

    async def scenario():
        await chat._cleanup_turn(Turn(), hub, "u", "s", "g", "", "stopped")
        await asyncio.sleep(0.2)  # let the scheduled settle task finish
        return hub.get_acp_manager("u", "s", "g")

    assert run(scenario()) is None
