"""Cancellation-safe terminal persistence and lease release."""
from __future__ import annotations

import asyncio

import pytest


def run(coro):
    return asyncio.run(coro)


def test_cancel_terminal_finalizes_and_persists_in_background():
    from app.routers import chat

    async def drain_background():
        while chat._BACKGROUND_TASKS:
            await asyncio.gather(*list(chat._BACKGROUND_TASKS), return_exceptions=True)

    async def scenario():
        calls: list[tuple[str, str]] = []

        def finalize(status: str) -> None:
            calls.append(("finalize", status))

        async def persist(status: str) -> None:
            await asyncio.sleep(0)
            calls.append(("persist", status))

        async def worker():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                chat._finalize_and_persist_after_cancel(finalize, persist, "stopped")
                raise

        task = asyncio.create_task(worker())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await drain_background()
        return calls

    assert run(scenario()) == [("finalize", "stopped"), ("persist", "stopped")]


def test_release_generation_after_cancel_is_retried_in_background():
    from app.routers import chat

    async def drain_background():
        while chat._BACKGROUND_TASKS:
            await asyncio.gather(*list(chat._BACKGROUND_TASKS), return_exceptions=True)

    async def scenario():
        calls: list[str] = []

        async def release():
            await asyncio.sleep(0)
            calls.append("released")

        async def worker():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await chat._release_or_background(release)
                raise

        task = asyncio.create_task(worker())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await drain_background()
        return calls

    assert run(scenario()) == ["released"]


def test_cleanup_acp_cancellation_logs_without_traceback(monkeypatch, caplog):
    import logging

    from app.routers import chat

    class Acp:
        def stop_all_now(self):
            return None

        async def close_all(self):
            raise asyncio.CancelledError

    class Host:
        def stop_all_now(self):
            return None

        async def close_all(self):
            return None

    class Turn:
        acp = Acp()
        host = Host()
        team = None

    class Hub:
        def unregister_subagent_host(self, *args):
            return None

        def unregister_acp_manager(self, *args):
            return None

    async def noop_settle(*args, **kwargs):
        return None

    monkeypatch.setattr(chat, "_settle_turn", noop_settle)
    caplog.set_level(logging.INFO)

    run(chat._cleanup_turn(Turn(), Hub(), "u", "s", "g", "", "stopped"))

    records = [r for r in caplog.records if "close ACP sessions" in r.message]
    assert records
    assert all(r.exc_info is None for r in records)
