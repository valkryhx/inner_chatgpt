"""ACP stdio 传输层：启动 GA 子进程、收发换行分隔的 JSON-RPC 2.0。

本模块只做进程与管道，不解析协议语义：

- 管道一律二进制（``text=True`` 会让 Windows 把换行翻成 CRLF）；
- stdout 读线程只负责 ``decode_line`` 并通过 ``loop.call_soon_threadsafe`` 投递，
  **所有协议状态都在事件循环线程上**；
- stderr 读线程**无条件**抽干管道（GA 对每条消息都打 ``[ACP-BRIDGE] ...``，
  管道写满就会把它卡死），只保留末尾 ``stderr_lines`` 行供报错时回显。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any

__all__ = [
    "AcpTransportClosed",
    "AsyncProcHandle",
    "AcpProcess",
    "decode_line",
    "kill_tree_now",
]

# Windows 上不要给子进程弹控制台窗口。
_CREATE_NO_WINDOW = 0x08000000

# 读线程一次预读的字节数（bufsize=0 时管道是 RawIOBase，逐字节 readline 太慢）。
_READ_BUFFER = 65536

# kill 之后再等这么久收尸。
_KILL_WAIT_S = 2.0

# 同步杀树后最多等这么久让 ``Popen`` 真的回收（同步路径不能 await）。
_SYNC_KILL_REAP_S = 2.0

# 同步杀树的回收轮询间隔。
_SYNC_KILL_POLL_S = 0.02


class AcpTransportClosed(RuntimeError):
    """传输层已经不可用（未启动 / 子进程退出 / 写入失败）。"""


def decode_line(raw: bytes) -> dict[str, Any] | None:
    """把一行原始字节解成 JSON 对象；空行、非法 JSON、非对象一律返回 ``None``。"""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:  # JSONDecodeError 是 ValueError 的子类
        return None
    return obj if isinstance(obj, dict) else None


def _buffered(stream: Any) -> Any:
    """给 RawIOBase（``bufsize=0`` 的管道）套一层读缓冲，已有缓冲原样返回。"""
    if isinstance(stream, io.RawIOBase):
        return io.BufferedReader(stream, buffer_size=_READ_BUFFER)
    return stream


class AsyncProcHandle:
    """``subprocess.Popen`` 的异步外观。

    ``ProcessLease.proc`` 里的消费者会 ``await proc.wait()``，所以 wait 必须是协程；
    子进程阻塞等待不能占住事件循环，走 ``asyncio.to_thread``。
    """

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def poll(self) -> int | None:
        return self._proc.poll()

    def kill(self) -> None:
        """强杀子进程；进程已退出时静默忽略。"""
        try:
            self._proc.kill()
        except OSError:
            pass

    async def wait(self) -> int:
        """等待子进程退出并返回退出码（在后台线程里阻塞）。"""
        return await asyncio.to_thread(self._proc.wait)


def kill_tree_now(handle: AsyncProcHandle | None) -> None:
    """Synchronous best-effort kill used from cancel-safe cleanup paths.

    用户点停止 / 本轮结束这类路径不允许 ``await``，只能在这里同步杀：

    - Windows 用 ``taskkill /T /F`` 杀整棵树（GA 会再拉起子进程）；
    - POSIX 用 ``killpg``，拿不到进程组时退回 ``kill()``。

    杀完必须轮询到 ``Popen`` 真正回收：``subprocess`` 不会自己更新
    ``returncode``，只有 ``poll()`` / ``wait()`` 会。不轮询的话调用方看到的
    ``returncode`` 永远是 ``None``，既判断不了“停掉了”，也留着僵尸进程。
    等待上限 ``_SYNC_KILL_REAP_S``，正常情况下 taskkill 返回时进程已被回收。
    """
    if handle is None or handle.returncode is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(handle.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(handle.pid), 9)
        except OSError:
            handle.kill()
    deadline = time.monotonic() + _SYNC_KILL_REAP_S
    while handle.poll() is None and time.monotonic() < deadline:
        time.sleep(_SYNC_KILL_POLL_S)


class AcpProcess:
    """一个 GA 子进程 + 它的 stdin/stdout/stderr 三条管道。"""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stderr_lines: int = 200,
    ) -> None:
        self._command = list(command)
        self._cwd = cwd
        self._env = dict(env)
        self._stderr_maxlen = max(int(stderr_lines), 1)
        self._stderr_tail: deque[str] = deque(maxlen=self._stderr_maxlen)
        # deque 本身的 append 是原子的，但迭代不是：快照必须与写入互斥。
        self._stderr_lock = threading.Lock()

        self._proc: subprocess.Popen[bytes] | None = None
        self._handle: AsyncProcHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._threads: list[threading.Thread] = []

        self._message_handler: Callable[[dict[str, Any]], None] | None = None
        self._close_handler: Callable[[BaseException], None] | None = None

        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_done = False

        self._closed = False
        self._close_notified = False

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        """启动子进程并开始抽干 stdout / stderr。

        必须在事件循环线程里调用：读线程投递消息依赖启动时捕获的 loop。
        """
        if self._proc is not None:
            raise RuntimeError("AcpProcess 已经启动过")
        self._loop = asyncio.get_running_loop()
        flags = _CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(
            self._command,
            cwd=self._cwd,
            env=self._env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=flags,
        )
        self._proc = proc
        self._handle = AsyncProcHandle(proc)
        self._spawn_reader("acp-stdout", self._read_stdout, proc.stdout)
        self._spawn_reader("acp-stderr", self._read_stderr, proc.stderr)

    def set_message_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """注册消息回调；回调在事件循环线程里执行。"""
        self._message_handler = handler

    def set_close_handler(self, handler: Callable[[BaseException], None]) -> None:
        """注册关闭回调（stdout EOF 时触发一次）。"""
        self._close_handler = handler

    @property
    def proc(self) -> AsyncProcHandle | None:
        return self._handle

    def stderr_tail(self, n: int = 20) -> list[str]:
        """返回 stderr 的最后 ``n`` 行。"""
        if n <= 0:
            return []
        with self._stderr_lock:
            return list(self._stderr_tail)[-n:]

    # ------------------------------------------------------------------ 读写

    async def send(self, msg: dict[str, Any]) -> None:
        """把一条消息编码成一行 UTF-8 JSON 写给子进程。"""
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AcpTransportClosed("ACP 子进程尚未启动")
        if self._loop is None:  # start() 在循环外调用的兜底绑定
            self._loop = asyncio.get_running_loop()
        payload = (
            json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        async with self._write_lock:
            if self._closed or proc.poll() is not None:
                raise AcpTransportClosed("ACP 子进程已退出，无法写入")
            try:
                await asyncio.to_thread(self._write, proc, payload)
            except (OSError, ValueError) as exc:
                raise AcpTransportClosed(f"写入 ACP 子进程失败：{exc}") from exc

    @staticmethod
    def _write(proc: subprocess.Popen[bytes], payload: bytes) -> None:
        stdin = proc.stdin
        if stdin is None:
            raise ValueError("stdin 已关闭")
        stdin.write(payload)
        stdin.flush()

    async def close(self, grace_s: float = 1.0) -> None:
        """关 stdin → 宽限等待 → kill → 再等 2 秒。可重复调用。"""
        async with self._close_lock:
            if self._close_done:
                return
            self._close_done = True
            self._closed = True
            proc, handle = self._proc, self._handle
            if proc is None or handle is None:
                return
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except (OSError, ValueError):
                pass
            if await self._wait_quietly(handle, grace_s):
                return
            handle.kill()
            await self._wait_quietly(handle, _KILL_WAIT_S)

    @staticmethod
    async def _wait_quietly(handle: AsyncProcHandle, timeout: float) -> bool:
        try:
            await asyncio.wait_for(handle.wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        except OSError:
            return True
        return True

    # ---------------------------------------------------------------- 读线程

    def _spawn_reader(
        self,
        name: str,
        target: Callable[[Any], None],
        stream: Any,
    ) -> None:
        thread = threading.Thread(target=target, args=(stream,), name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _read_stdout(self, stream: Any) -> None:
        reader = None
        try:
            reader = _buffered(stream)
            while True:
                raw = reader.readline()
                if not raw:
                    break
                msg = decode_line(raw)
                if msg is not None:
                    self._post(self._dispatch, msg)
        except (OSError, ValueError):
            pass
        finally:
            _close_quietly(reader)
            self._post(self._notify_closed, AcpTransportClosed("ACP 子进程的标准输出已关闭"))

    def _read_stderr(self, stream: Any) -> None:
        # 必须无条件抽干：GA 对每条消息都打日志，管道满即死锁。
        reader = None
        try:
            reader = _buffered(stream)
            while True:
                raw = reader.readline()
                if not raw:
                    break
                with self._stderr_lock:
                    self._stderr_tail.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            _close_quietly(reader)

    # ------------------------------------------------------- 循环线程上的回调

    def _post(self, fn: Callable[[Any], None], arg: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(fn, arg)
        except RuntimeError:  # 检查与投递之间 loop 被关掉
            pass

    def _dispatch(self, msg: dict[str, Any]) -> None:
        handler = self._message_handler
        if handler is not None:
            handler(msg)

    def _notify_closed(self, exc: BaseException) -> None:
        if self._close_notified:
            return
        self._close_notified = True
        self._closed = True
        handler = self._close_handler
        if handler is not None:
            handler(exc)


def _close_quietly(stream: Any) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass
