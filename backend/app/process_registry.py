"""Active code_run processes, scoped so stop never kills another user's work."""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ProcessLease:
    user_id: str
    session_id: str
    generation_id: str
    agent_id: str
    proc: Any | None = None
    stopped: bool = False


_leases: dict[tuple[str, str, str], list[ProcessLease]] = {}
_lock = asyncio.Lock()


class _ThreadProcess:
    """Minimal asyncio.subprocess-like wrapper for Windows SelectorEventLoop.

    Uvicorn enables reload by creating a SelectorEventLoop on Windows.  Its
    subprocess APIs are intentionally unimplemented.  ``Popen.communicate``
    still works from a worker thread, so keep the public surface used by
    code_run (pid/returncode/kill/communicate/wait) identical.
    """

    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return int(self._process.pid)

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return await asyncio.to_thread(self._process.communicate)

    async def wait(self) -> int:
        return await asyncio.to_thread(self._process.wait)

    def kill(self) -> None:
        self._process.kill()


async def spawn_process(command: list[str], **kwargs: Any) -> Any:
    """Create a subprocess on both Proactor and Selector Windows loops."""
    loop_name = type(asyncio.get_running_loop()).__name__
    if os.name == "nt" and loop_name != "ProactorEventLoop":
        return _ThreadProcess(subprocess.Popen(command, **kwargs))
    try:
        return await asyncio.create_subprocess_exec(*command, **kwargs)
    except (NotImplementedError, RuntimeError):
        if os.name != "nt":
            raise
        return _ThreadProcess(subprocess.Popen(command, **kwargs))


async def register(lease: ProcessLease) -> None:
    async with _lock:
        _leases.setdefault((lease.user_id, lease.session_id, lease.generation_id), []).append(lease)


async def unregister(lease: ProcessLease) -> None:
    async with _lock:
        key = (lease.user_id, lease.session_id, lease.generation_id)
        items = _leases.get(key)
        if items is not None:
            items.remove(lease) if lease in items else None
            if not items:
                _leases.pop(key, None)


async def _kill_tree(proc: Any) -> None:
    if proc.returncode is not None:
        return
    if os.name == "nt":
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except Exception:
        pass


async def stop_generation(user_id: str, session_id: str, generation_id: str) -> int:
    async with _lock:
        items = list(_leases.get((user_id, session_id, generation_id), []))
    count = 0
    for lease in items:
        lease.stopped = True
        if lease.proc is not None:
            await _kill_tree(lease.proc)
            count += 1
    return count


async def stop_all() -> int:
    async with _lock:
        items = [lease for group in _leases.values() for lease in group]
    for lease in items:
        lease.stopped = True
        if lease.proc is not None:
            await _kill_tree(lease.proc)
    return len(items)
