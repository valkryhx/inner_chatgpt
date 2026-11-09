import asyncio
import io
import os
import sys
from pathlib import Path

from app.acp import transport as transport_mod
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



def test_stderr_tail_snapshot_is_race_free():
    """写线程持续压行、主线程反复取快照：不许抛 RuntimeError(deque mutated)。"""
    import io
    import threading

    p = AcpProcess([sys.executable, "-c", "pass"], cwd=str(Path.cwd()),
                   env={}, stderr_lines=5000)
    read_fd, write_fd = os.pipe()
    stop = threading.Event()

    def write_lines():
        i = 0
        while not stop.is_set():
            try:
                os.write(write_fd, b"line %d\n" % i)
            except OSError:      # 主线程已关写端
                return
            i += 1

    reader = threading.Thread(target=p._read_stderr,
                              args=(io.FileIO(read_fd, "rb", closefd=True),),
                              name="test-stderr", daemon=True)
    writer = threading.Thread(target=write_lines, name="test-stderr-writer", daemon=True)

    # 默认 GIL 切换间隔 5ms，撞上 deque 迭代的窗口太小；压到 1us 把窗口放大
    # （free-threaded 构建上这条测试才有机会真的复现 RuntimeError）。
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    reader.start()
    writer.start()
    try:
        for _ in range(2000):
            tail = p.stderr_tail(10)
            assert len(tail) <= 10
    finally:
        stop.set()
        writer.join(5)
        os.close(write_fd)
        reader.join(5)
        sys.setswitchinterval(old_interval)
    assert 0 < len(p.stderr_tail(10)) <= 10


def test_spawn_isolates_the_child_into_its_own_session(monkeypatch):
    """POSIX 必须 start_new_session：kill_tree_now 用 killpg 杀整组，
    子进程若与服务端同组，一次停 GA 就会把服务端自己 SIGKILL 掉。
    Windows 仍然只设 creationflags（不弹窗），不传 start_new_session。"""
    calls = []

    class FakeProc:
        pid = 4242
        returncode = None

        def __init__(self):
            self.stdin = None
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")

    def fake_popen(command, **kwargs):
        calls.append(kwargs)
        return FakeProc()

    monkeypatch.setattr(transport_mod.subprocess, "Popen", fake_popen)

    async def start_with(name):
        monkeypatch.setattr(transport_mod.os, "name", name)
        proc = AcpProcess(["fake-agent"], cwd=".", env={})
        proc.start()
        await asyncio.sleep(0)
        return dict(calls[-1])

    posix = run(start_with("posix"))
    assert posix["start_new_session"] is True
    assert "creationflags" not in posix

    windows = run(start_with("nt"))
    assert windows["creationflags"] == transport_mod._CREATE_NO_WINDOW
    assert "start_new_session" not in windows
