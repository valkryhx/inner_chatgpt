"""ACP JSON-RPC 客户端：请求/响应关联、通知分发、权限自动应答。

本模块只处理协议语义，进程与管道全部交给
:class:`app.acp.transport.AcpProcess`。

线程模型：传输层保证消息回调通过 ``loop.call_soon_threadsafe`` 投递，
因此本模块的所有状态（``_pending`` / ``_next_id`` / ``_active_prompt``）
只在事件循环线程上读写，无需加锁。
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from typing import Any

from .transport import AcpProcess, AcpTransportClosed

__all__ = ["AcpBusyError", "AcpClient", "AcpError"]

# 握手 / 建会话 / 关会话这类控制请求的默认超时；prompt 由调用方显式传。
_CONTROL_TIMEOUT_S = 30.0

# JSON-RPC 2.0 标准错误码：方法不存在。
_METHOD_NOT_FOUND = -32601

# 握手参数固定：GA 只认 protocolVersion 1。
_INITIALIZE_PARAMS: dict[str, Any] = {
    "protocolVersion": 1,
    "clientCapabilities": {},
    "clientInfo": {"name": "inner_chatgpt", "version": "1.0"},
}


class AcpError(RuntimeError):
    """agent 返回的 JSON-RPC 错误（``error`` 对象原样拆开）。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class AcpBusyError(RuntimeError):
    """同一客户端已有 active prompt 时再次派任务。

    设计文档 §6.4：本地先拒绝，不依赖对端 ``-32603``。
    """


class AcpClient:
    """一条 ACP 连接上的协议客户端（一个 GA 子进程一条连接）。"""

    def __init__(
        self,
        process: AcpProcess,
        *,
        permission: str = "allow",
        on_update: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._process = process
        self._permission = permission
        self._on_update = on_update

        self._next_id = 1
        self._pending: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        self._active_prompt: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # 强引用住「发出去就不管」的任务，否则可能被 GC 掉。
        self._tasks: set[asyncio.Task[None]] = set()

        self.permission_requests: list[dict[str, Any]] = []
        self.agent_info: dict[str, Any] = {}

        process.set_message_handler(self._dispatch)
        process.set_close_handler(self._on_close)

    # ------------------------------------------------------------- 对外方法

    async def start(self) -> dict[str, Any]:
        """握手，返回 ``initialize`` 的完整 result。"""
        self._loop = asyncio.get_running_loop()
        result = await self._request(
            "initialize", dict(_INITIALIZE_PARAMS), _CONTROL_TIMEOUT_S
        )
        info = result.get("agentInfo")
        self.agent_info = info if isinstance(info, dict) else {}
        version = result.get("protocolVersion")
        if version != 1:
            # 设计文档 §6.2：记录告警但继续，GA 总是回 1。
            self._log(f"initialize 返回 protocolVersion={version!r}，期望 1")
        return result

    async def new_session(self, cwd: str) -> dict[str, Any]:
        """建会话；``cwd`` 必须由服务端拼好，不接受模型传入。"""
        return await self._request(
            "session/new", {"cwd": cwd, "mcpServers": []}, _CONTROL_TIMEOUT_S
        )

    async def set_mode(self, session_id: str, mode_id: str) -> dict[str, Any]:
        """切换会话模式（``full_access`` / ``ask`` / ``read_only``）。"""
        return await self._request(
            "session/set_mode",
            {"sessionId": session_id, "modeId": mode_id},
            _CONTROL_TIMEOUT_S,
        )

    async def prompt(
        self, session_id: str, text: str, *, timeout_s: float
    ) -> dict[str, Any]:
        """派任务并等待 ``stopReason``。

        同一客户端同时只允许一个 prompt；超时抛 ``asyncio.TimeoutError``，
        取消/超时都会在 ``finally`` 里清空 busy 标记。
        """
        if self._active_prompt is not None:
            raise AcpBusyError(
                f"session {self._active_prompt!r} 仍有未完成的 prompt，拒绝并发派任务"
            )
        self._active_prompt = session_id
        try:
            return await self._request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout_s,
            )
        finally:
            self._active_prompt = None

    async def cancel(self, session_id: str) -> None:
        """发 ``session/cancel`` 通知（没有 id，不等待响应）。"""
        await self._process.send(
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": session_id},
            }
        )

    async def close_session(self, session_id: str) -> dict[str, Any]:
        """``session/close`` 请求；GA 会顺带 request_shutdown。"""
        return await self._request(
            "session/close", {"sessionId": session_id}, _CONTROL_TIMEOUT_S
        )

    async def close(self) -> None:
        """关闭底层传输（关 stdin → 宽限 → kill）。"""
        await self._process.close()

    # ------------------------------------------------------------- 请求关联

    async def _request(
        self, method: str, params: dict[str, Any], timeout_s: float
    ) -> dict[str, Any]:
        loop = self._loop
        if loop is None:  # start() 之前在循环里被调用时的兜底
            loop = asyncio.get_running_loop()
            self._loop = loop
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[rid] = fut
        try:
            await self._process.send(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            )
            return await asyncio.wait_for(fut, timeout_s)
        finally:
            # 超时 / 取消 / 发送失败都必须把槽位收干净。
            self._pending.pop(rid, None)
            if not fut.done():
                fut.cancel()

    # --------------------------------------------------------------- 分发

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """入站三分：请求、通知、响应。运行在事件循环线程上。"""
        method = msg.get("method")
        rid = msg.get("id")
        if method is not None:
            params = msg.get("params")
            if not isinstance(params, dict):
                params = {}
            if rid is not None:
                self._handle_request(rid, method, params)
            else:
                self._handle_notification(method, params)
            return
        if rid is not None:
            self._handle_response(rid, msg)

    def _handle_request(self, rid: Any, method: str, params: dict[str, Any]) -> None:
        if method == "session/request_permission":
            self.permission_requests.append(params)
            option = "allow_once" if self._permission == "allow" else "reject_once"
            self._send_soon(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"outcome": {"outcome": "selected", "optionId": option}},
                }
            )
            return
        # 其它方法一律按 JSON-RPC 规范回 -32601，绝不能静默丢弃。
        self._send_soon(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": _METHOD_NOT_FOUND, "message": "Method not found"},
            }
        )

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "session/update":
            return
        update = params.get("update")
        if not isinstance(update, dict):
            self._log(f"session/update 缺少合法 update：{params!r}")
            return
        callback = self._on_update
        if callback is None:
            return
        session_id = params.get("sessionId")
        try:
            callback(session_id, update)
        except Exception as exc:  # noqa: BLE001 - 回调异常不能打断读循环
            self._log(f"on_update 回调异常：{exc!r}")

    def _handle_response(self, rid: Any, msg: dict[str, Any]) -> None:
        fut = self._pending.get(rid)
        if fut is None or fut.done():
            return  # 超时后才到的响应：直接丢弃
        error = msg.get("error")
        if isinstance(error, dict):
            fut.set_exception(
                AcpError(
                    int(error.get("code", 0) or 0),
                    str(error.get("message", "")),
                    error.get("data"),
                )
            )
            return
        result = msg.get("result")
        fut.set_result(result if isinstance(result, dict) else {})

    def _on_close(self, exc: BaseException) -> None:
        """传输关闭：把所有等着的请求全部失败掉，别让调用方挂死。"""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)

    # --------------------------------------------------------------- 小工具

    def _send_soon(self, msg: dict[str, Any]) -> None:
        """在同步回调里发消息：排一个任务，失败只记日志。"""
        try:
            task = asyncio.create_task(self._send_quietly(msg))
        except RuntimeError as exc:  # 没有运行中的事件循环
            self._log(f"无法发送 ACP 消息：{exc!r}")
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_quietly(self, msg: dict[str, Any]) -> None:
        try:
            await self._process.send(msg)
        except AcpTransportClosed as exc:
            self._log(f"发送 ACP 消息失败：{exc}")

    @staticmethod
    def _log(text: str) -> None:
        """协议层告警走 stderr，不污染 stdout 的 JSON-RPC 流。"""
        try:
            sys.stderr.write(f"[acp-client] {text}\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
