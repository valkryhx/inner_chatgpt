import asyncio
import os
import sys
from pathlib import Path

from app.acp.transport import AcpProcess, AsyncProcHandle, decode_line

FIXTURE = str(Path(__file__).parent / "fixtures" / "fake_ga_bridge.py")


def run(coro):
    return asyncio.run(coro)


def start(mode="chunks", **env):
    proc = AcpProcess([sys.executable, FIXTURE], cwd=str(Path.cwd()),
                      env={**os.environ, "FAKE_GA_MODE": mode, **env})
    proc.start()
    return proc


def test_decode_line_strips_crlf_and_ignores_garbage():
    assert decode_line(b'{"a":1}\r\n') == {"a": 1}
    assert decode_line(b"not json\n") is None
    assert decode_line(b"\n") is None


def test_roundtrip_response():
    async def scenario():
        p = start()
        got = asyncio.get_running_loop().create_future()
        p.set_message_handler(lambda m: got.done() or got.set_result(m))
        try:
            await p.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            return await asyncio.wait_for(got, 10)
        finally:
            await p.close()

    msg = run(scenario())
    assert msg["id"] == 1
    assert msg["result"]["agentInfo"]["name"] == "fake-ga"


def test_split_write_is_reassembled():
    async def scenario():
        p = start("split")
        got = asyncio.get_running_loop().create_future()
        p.set_message_handler(lambda m: got.done() or got.set_result(m))
        try:
            await p.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            return await asyncio.wait_for(got, 10)
        finally:
            await p.close()

    assert run(scenario())["result"]["agentInfo"]["name"] == "fake-ga"


def test_stderr_flood_does_not_block():
    async def scenario():
        p = start(FAKE_GA_STDERR_LINES="20000")   # > 64KB of stderr before reading stdin
        got = asyncio.get_running_loop().create_future()
        p.set_message_handler(lambda m: got.done() or got.set_result(m))
        try:
            await p.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            return await asyncio.wait_for(got, 10)
        finally:
            await p.close()

    assert run(scenario())["id"] == 1


def test_close_terminates_process_and_wait_is_async():
    async def scenario():
        p = start()
        handle = p.proc
        assert isinstance(handle, AsyncProcHandle)
        await p.close(grace_s=0.2)
        return await asyncio.wait_for(handle.wait(), 5)

    assert isinstance(run(scenario()), int)


def test_unknown_line_does_not_kill_reader():
    async def scenario():
        p = start("malformed")
        msgs = []
        done = asyncio.get_running_loop().create_future()

        def handler(m):
            msgs.append(m)
            if not done.done():
                done.set_result(True)

        p.set_message_handler(handler)
        try:
            await p.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            await asyncio.wait_for(done, 10)
            assert msgs[0]["id"] == 1
        finally:
            await p.close()

    run(scenario())
