"""GaSession：一次 GA 会话的状态机、ACP 事件映射与结果聚合。

职责边界（见 docs/20261109-ACP-client驱动GenericAgent设计与实施.md §5.3、§8）：

- 进程与管道交给 :class:`app.acp.transport.AcpProcess`；
- 协议语义（请求关联 / 权限应答）交给 :class:`app.acp.client.AcpClient`；
- 本模块只做三件事：把 ``session/update`` 翻成现有的 ``subagent_*`` 事件、
  维护 ``full_text`` / ``result`` 两份文本、把一次 prompt 的结局收敛成
  :class:`PromptResult`。

事件日志（``tail``）用的是本会话自己的单调 ``seq``，与 ``EventBuffer`` 的
序号无关——``ga_wait`` 的 ``since_event_seq`` 就是拿它做增量。
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .client import AcpBusyError, AcpClient, AcpError
from .config import AcpSettings
from .transport import AcpProcess, AcpTransportClosed, AsyncProcHandle

__all__ = ["GaSession", "PromptResult"]

# 每会话事件日志上限（与 SubagentHost 的环形缓冲同一量级）。
_EVENT_LOG_MAX = 2000

# tail() 里单条文本的截断长度。
_TEXT_LIMIT = 400

# 停一条已死进程时的兜底等待，不能把停止路径挂死。
_CANCEL_TIMEOUT_S = 5.0

# prompt 结果里代表“被取消”的 stopReason。
_CANCELLED_STOP_REASONS = frozenset({"cancelled", "canceled"})

# tool_call_update 里代表失败的 status。
_FAILED_TOOL_STATUSES = frozenset({"failed", "error"})

# 整个 prompt 没有任何文本事件时，subagent_end 带的占位结果。
_NO_TEXT_RESULT = "（GA 未返回文本）"

# 终态集合：进入后不再接受任何 update（spec §9.2：终态只允许进入一次）。
_TERMINAL_STATUSES = frozenset({"completed", "errored", "stopped"})


@dataclass
class PromptResult:
    """一次 ``session/prompt`` 的结局。"""

    status: str
    result: str = ""
    full_text: str = ""
    stop_reason: str = ""
    tool_calls: int = 0
    error: str = ""


class GaSession:
    """一条 ACP 连接 + 它的会话状态。

    一个实例对一个 GA 子进程；``agent_id`` 是会话级 id（``ga_<hex>``），
    每次 prompt 用的卡片 id 是 ``f"{agent_id}#{prompt_seq}"``。
    """

    def __init__(
        self,
        agent_id: str,
        title: str,
        task: str,
        *,
        settings: AcpSettings,
        cwd: str,
        env: Mapping[str, str],
        publish: Callable[[dict[str, Any]], None],
    ) -> None:
        self.agent_id = agent_id
        self.title = title or ""
        self.task = task
        self.settings = settings
        self.cwd = cwd
        self.env = dict(env)
        self._publish = publish

        self.status = "starting"
        self.error = ""
        self.result = ""
        self.full_text = ""
        self.process_handle: AsyncProcHandle | None = None

        self._process: AcpProcess | None = None
        self._client: AcpClient | None = None
        self._session_id: str | None = None
        self._card_id = ""
        self._prompt_seq = 0
        self._prompts = 0
        self._tool_calls = 0
        self._seq = 0
        self._event_log: deque[dict[str, Any]] = deque(maxlen=_EVENT_LOG_MAX)

        # 每次 prompt 重算的两份文本：全量 chunk 与「最后一个工具事件之后」的后缀。
        self._full_parts: list[str] = []
        self._tail_parts: list[str] = []
        self._saw_text = False
        self._stop_requested = False
        self._in_flight = False
        self._ended = False
        self._dropped = 0

    # ------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        """起子进程 → 握手 → 建会话 →（必要时）切模式。"""
        if self._client is not None:
            raise RuntimeError("GaSession 已经启动过")
        if not self.settings.command:
            raise RuntimeError("ACP 未启用：INNER_ACP_GA_COMMAND 为空")

        process = AcpProcess(
            self.settings.command,
            cwd=self.cwd,
            env=self.env,
            stderr_lines=self.settings.stderr_lines,
        )
        process.start()
        self._process = process
        self.process_handle = process.proc

        client = AcpClient(
            process,
            permission=self.settings.permission,
            on_update=self._on_update,
            on_permission=self._on_permission,
        )
        self._client = client
        await client.start()

        info = await client.new_session(self.cwd)
        session_id = info.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError(f"session/new 未返回 sessionId：{info!r}")
        self._session_id = session_id

        modes = info.get("modes")
        current = modes.get("currentModeId") if isinstance(modes, dict) else None
        if self.settings.mode and self.settings.mode != current:
            await client.set_mode(session_id, self.settings.mode)
        self.status = "running"

    async def stop(self) -> None:
        """发 ``session/cancel`` 并把状态标成 stopped；死进程不报错。"""
        if self._stop_requested:
            return
        self._stop_requested = True
        self.status = "stopped"
        await self._cancel_quietly()

    async def close(self) -> None:
        """关闭底层传输。进程句柄保留，供租约用到 ``close_all``。"""
        client = self._client
        if client is None:
            return
        await client.close()

    # ------------------------------------------------------------------ 派活

    async def prompt(
        self, text: str, *, prompt_seq: int, timeout_s: float
    ) -> PromptResult:
        """发一次 prompt；无论成败都会发一条 ``subagent_end``。"""
        client, session_id = self._client, self._session_id
        if client is None or session_id is None:
            raise RuntimeError("GaSession 尚未启动")
        if self._in_flight:
            # 并发派任务在本地就拒掉：不改状态、不发新卡片，上一张卡片继续跑。
            raise AcpBusyError(
                f"会话 {self.agent_id} 仍有未完成的 prompt，拒绝并发派任务"
            )

        self._in_flight = True
        try:
            self._prompt_seq = int(prompt_seq)
            self._card_id = f"{self.agent_id}#{self._prompt_seq}"
            self._full_parts = []
            self._tail_parts = []
            self._saw_text = False
            self._stop_requested = False
            self._ended = False
            self.full_text = ""
            self.result = ""
            self.error = ""
            self.status = "running"

            self._publish(
                {
                    "type": "subagent_start",
                    "agent_id": self._card_id,
                    "name": self._display_name(),
                    "role": "ga",
                    "task": self.task,
                }
            )

            stop_reason = ""
            try:
                result = await client.prompt(session_id, text, timeout_s=timeout_s)
                stop_reason = str(result.get("stopReason") or "")
                if self._stop_requested or stop_reason in _CANCELLED_STOP_REASONS:
                    self.status = "stopped"
                else:
                    self.status = "completed"
            except asyncio.TimeoutError:
                self.error = f"timeout: 超过 {timeout_s:g}s"
                stop_reason = "timeout"
                self.status = "stopped"
                await self._cancel_quietly()
            except AcpTransportClosed as exc:
                if self._stop_requested:
                    # 用户停止 / 关管道导致的正常收尾，不是故障（spec §6.6 / §9.4）。
                    self.status = "stopped"
                else:
                    self.error = f"{type(exc).__name__}: {exc}"
                    self.status = "errored"
            except AcpError as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self.status = "errored"
            except asyncio.CancelledError:
                self.error = self.error or "cancelled"
                self.status = "stopped"
                raise
            except Exception as exc:  # noqa: BLE001 - 任何异常都必须收敛成终态
                self.error = f"{type(exc).__name__}: {exc}"
                self.status = "errored"

            self._finish_prompt()
            return PromptResult(
                status=self.status,
                result=self.result,
                full_text=self.full_text,
                stop_reason=stop_reason,
                tool_calls=self._tool_calls,
                error=self.error,
            )
        finally:
            self._in_flight = False
            # 异常路径也必须收尾；_finish_prompt 幂等，正常路径不会重复发。
            self._finish_prompt()

    # ------------------------------------------------------------- 查询接口

    @property
    def last_seq(self) -> int:
        """本会话事件日志的当前序号（单调自增，不随日志裁剪回退）。"""
        return self._seq

    def snapshot(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "status": self.status,
            "result": self.result,
            "full_text": self.full_text,
            "tool_calls": self._tool_calls,
            "prompts": self._prompts,
            "error": self.error,
            "last_seq": self._seq,
        }

    def tail(self, since_seq: int, limit: int = 8) -> list[dict[str, Any]]:
        """取 ``seq > since_seq`` 的最近 ``limit`` 条事件日志。"""
        try:
            limit = int(limit)
            since = int(since_seq)
        except (TypeError, ValueError):
            return []
        if limit <= 0:
            return []
        rows = [dict(entry) for entry in self._event_log if entry["seq"] > since]
        return rows[-limit:]

    # ------------------------------------------------------------- 事件映射

    def _on_update(self, session_id: str | None, update: dict[str, Any]) -> None:
        """把一条 ``session/update`` 翻成 buffer 事件 + 本地日志。"""
        if self.status in _TERMINAL_STATUSES:
            # spec §9.2：终态只允许进入一次，迟到的事件丢弃并记日志。
            self._dropped += 1
            self._log(
                f"终态 {self.status} 后丢弃 update：{update.get('sessionUpdate')!r}"
            )
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            self._on_text_chunk(update)
        elif kind == "tool_call":
            self._on_tool_call(update)
        elif kind == "tool_call_update":
            self._on_tool_call_update(update)

    def _on_text_chunk(self, update: dict[str, Any]) -> None:
        text = _chunk_text(update)
        if not text:
            return
        self._saw_text = True
        self._full_parts.append(text)
        self._tail_parts.append(text)
        self._append_log("text", text=text)
        self._publish(
            {
                "type": "subagent_event",
                "agent_id": self._card_id,
                "event": {"type": "text_delta", "text": text},
            }
        )

    def _on_tool_call(self, update: dict[str, Any]) -> None:
        tool_id = str(update.get("toolCallId") or "")
        name = str(update.get("title") or "")
        raw_input = update.get("rawInput")
        if not isinstance(raw_input, dict):
            raw_input = {}
        self._tool_calls += 1
        # 工具事件之后的文本才算 result 的后缀。
        self._tail_parts = []
        self._append_log("tool_start", name=name, text=_brief_input(raw_input))
        self._publish(
            {
                "type": "subagent_event",
                "agent_id": self._card_id,
                "event": {
                    "type": "tool_start",
                    "id": tool_id,
                    "name": name,
                    "input": raw_input,
                },
            }
        )

    def _on_tool_call_update(self, update: dict[str, Any]) -> None:
        tool_id = str(update.get("toolCallId") or "")
        name = str(update.get("title") or "")
        status = str(update.get("status") or "")
        text = _tool_output_text(update)
        self._tail_parts = []
        self._append_log("tool_result", name=name, text=text)
        self._publish(
            {
                "type": "subagent_event",
                "agent_id": self._card_id,
                "event": {
                    "type": "tool_result",
                    "id": tool_id,
                    "name": name,
                    "content": text,
                    "is_error": status in _FAILED_TOOL_STATUSES,
                },
            }
        )

    def _on_permission(self, params: dict[str, Any]) -> None:
        """自动应答的权限请求：发一对 ``tool_start`` / ``tool_result``（spec §6.5）。

        缺了 ``tool_result``，``SubagentCard`` 的步骤会永远停在 running 圆点。
        """
        if self.status in _TERMINAL_STATUSES:
            self._dropped += 1
            self._log(f"终态 {self.status} 后丢弃 permission 请求")
            return
        tool = params.get("toolCall")
        if not isinstance(tool, dict):
            tool = {}
        tool_id = str(tool.get("toolCallId") or "")
        title = str(tool.get("title") or tool_id or "unknown")
        name = f"permission:{title}"
        allowed = self.settings.permission == "allow"
        content = "allowed" if allowed else "denied"
        raw_input = tool.get("rawInput")
        if not isinstance(raw_input, dict):
            raw_input = {}
        self._append_log("tool_start", name=name, text=_brief_input(raw_input))
        self._publish(
            {
                "type": "subagent_event",
                "agent_id": self._card_id,
                "event": {
                    "type": "tool_start",
                    "id": tool_id,
                    "name": name,
                    "input": raw_input,
                },
            }
        )
        self._append_log("tool_result", name=name, text=content)
        self._publish(
            {
                "type": "subagent_event",
                "agent_id": self._card_id,
                "event": {
                    "type": "tool_result",
                    "id": tool_id,
                    "name": name,
                    "content": content,
                    "is_error": not allowed,
                },
            }
        )

    @staticmethod
    def _log(text: str) -> None:
        """内部告警走 stderr，不污染 stdout 的 JSON-RPC 流。"""
        try:
            sys.stderr.write(f"[acp-session] {text}\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass

    # --------------------------------------------------------------- 内部

    def _finish_prompt(self) -> None:
        """收敛两份文本、累加 prompts、发 ``subagent_end``（幂等）。"""
        if self._ended:
            return
        self._ended = True
        self.full_text = "".join(self._full_parts)
        tail = "".join(self._tail_parts)
        self.result = tail or self.full_text
        self._prompts += 1
        end: dict[str, Any] = {
            "type": "subagent_end",
            "agent_id": self._card_id,
            "name": self._display_name(),
            "status": self.status,
            "turns": 0,
            "error": self.error,
        }
        if not self._saw_text:
            # 有文本事件时卡片时间线已经展示过，不再重复塞 result。
            end["result"] = _NO_TEXT_RESULT
        self._publish(end)

    def _display_name(self) -> str:
        return f"GenericAgent · {self.title}" if self.title else "GenericAgent"

    def _append_log(self, kind: str, *, name: str = "", text: str = "") -> None:
        self._seq += 1
        entry: dict[str, Any] = {"seq": self._seq, "kind": kind}
        if name:
            entry["name"] = name
        if text:
            entry["text"] = text[:_TEXT_LIMIT]
        self._event_log.append(entry)

    async def _cancel_quietly(self) -> None:
        client, session_id = self._client, self._session_id
        if client is None or session_id is None:
            return
        try:
            await asyncio.wait_for(client.cancel(session_id), _CANCEL_TIMEOUT_S)
        except (AcpTransportClosed, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001 - 停一条已死的进程不应抛给上层
            pass


# --------------------------------------------------------------- 报文小工具


def _chunk_text(update: dict[str, Any]) -> str:
    """``agent_message_chunk`` 里的文本；非文本块一律忽略。"""
    content = update.get("content")
    if isinstance(content, dict):
        if content.get("type") not in (None, "text"):
            return ""
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(content, str):
        return content
    return ""


def _tool_output_text(update: dict[str, Any]) -> str:
    """``tool_call_update`` 的可读输出：优先文本，没有文本就退回 status。"""
    parts: list[str] = []
    content = update.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                inner = item.get("content")
                if isinstance(inner, dict):
                    text = inner.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(inner, str):
                    parts.append(inner)
    elif isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            parts.append(text)
    elif isinstance(content, str):
        parts.append(content)
    text = "".join(parts)
    if text:
        return text
    return str(update.get("status") or "")


def _brief_input(raw_input: dict[str, Any]) -> str:
    """日志里给工具入参一个短摘要（优先 ``preview``，否则紧凑 JSON）。"""
    if not raw_input:
        return ""
    preview = raw_input.get("preview")
    if isinstance(preview, str) and preview:
        return preview
    try:
        return json.dumps(raw_input, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(raw_input)
