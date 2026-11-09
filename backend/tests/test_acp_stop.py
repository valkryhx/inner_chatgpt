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
