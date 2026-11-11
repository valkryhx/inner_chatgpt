"""5 个 GA 工具：``ga_spawn`` / ``ga_wait`` / ``ga_result`` / ``ga_send`` / ``ga_stop``。

工具契约见设计文档 ``docs/20261109-ACP-client驱动GenericAgent设计与实施.md`` §7：

- 只有 root agent 的 registry 注入它们（§7.1），子代理 / teammate 没有；
- 每个 ``call`` 都自己吞掉异常，返回 ``[工具执行错误] ...`` 文本，**永不向上抛**
  （与 :meth:`app.tools.ToolRegistry.dispatch` 的约定一致）；
- 返回值一律 ``json.dumps(..., ensure_ascii=False)``，模型侧看到的是 JSON 文本。
"""
from __future__ import annotations

import json
from typing import Any

from ..tools import Tool
from .manager import AcpSessionManager

_SPAWN_DESC = (
    "把一件可独立完成的子任务交给本机 GenericAgent（外部编码代理）。\n"
    "  - 任务书要自包含：目标、涉及路径、验收标准、禁止事项；它看不到当前对话。\n"
    "  - 它在本用户的 workspace 内工作，产物可直接用 file_read / code_run 复核。\n"
    "  - 派活后必须用 ga_wait / ga_result 收结果；本轮结束时未完成的任务会被停止。"
)

_WAIT_DESC = "等待 GA 完成。超时只表示还在跑，不会终止它。"
_RESULT_DESC = (
    "读取 GA 的最终答复与工具调用统计。\n"
    "  - 默认上限 50000 字符；不够就读不全，会明确告诉你全文多少字符。\n"
    "  - close=true 与截断互斥：结果会被截断时拒绝关闭，避免「没读完就销毁」。\n"
    "    要关就先读全，或显式传 force_close=true 丢弃剩余部分。\n"
    "  - 已关闭的会话仍可读（结果落库），返回 source=\"db\"。"
)
_SEND_DESC = "对同一 GA 会话追加指令（一次只能有一个进行中的 prompt）。"
_STOP_DESC = "停止 GA（先发 session/cancel，2 秒后强制结束进程树）。"

_WAIT_TIMEOUT_NOTE = "仍在运行；可继续 ga_wait，或先 ga_result 看中间文本"


def _error(exc: Exception) -> str:
    return f"[工具执行错误] {type(exc).__name__}: {exc}"


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


# 模型经常把布尔值写成字符串：``"false"`` / ``"0"`` / ``"no"`` / ``""`` 都必须当 False。
# 否则 ``close="false"`` 会被 ``bool()`` 判成 True，真的把会话名额释放掉（不可逆）。
_FALSE_STRINGS = frozenset({"false", "0", "no", ""})


def _as_bool(value: Any) -> bool:
    """宽进严出地解析布尔参数；真布尔、数字、None 走 Python 语义。"""
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_STRINGS
    return bool(value)


def _agent_id(args: dict) -> str | None:
    """``agent_id``：缺省 / 空串都表示 None（ga_wait、ga_stop 里 = 全部）。"""
    value = args.get("agent_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_acp_tools(manager: AcpSessionManager) -> list[Tool]:
    """构造 5 个 GA 工具的 ``Tool`` 定义（顺序即模型看到顺序）。"""

    async def ga_spawn(args: dict) -> str:
        try:
            task = str(args.get("task", "") or "").strip()
            if not task:
                # null / 空任务绝不能进 manager：那会白开一个会话并占住名额。
                raise ValueError("task 不能为空")
            out = await manager.spawn(
                task=task,
                title=str(args.get("title", "") or ""),
                mode=str(args.get("mode", "") or ""),
                # 0 = 用服务端 prompt_timeout_s（默认 1800s），不在工具层写死。
                timeout_s=float(args.get("timeout_s", 0) or 0),
            )
            return _dumps(out)
        except Exception as e:  # noqa: BLE001 - 工具永不抛
            return _error(e)

    async def ga_wait(args: dict) -> str:
        try:
            out = await manager.wait(
                _agent_id(args),
                timeout_s=float(args.get("timeout_s", 60) or 60),
                since_seq=int(args.get("since_event_seq", 0) or 0),
            )
            if isinstance(out, dict) and out.get("timed_out"):
                out = {**out, "note": _WAIT_TIMEOUT_NOTE}
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _error(e)

    async def ga_result(args: dict) -> str:
        try:
            agent_id = _agent_id(args)
            if not agent_id:
                raise ValueError("agent_id 不能为空")
            # force_close 是逃生口：只有当调用方明确知道剩余部分可以丢弃时才用。
            # 正常路径下「截断 + close」会被 manager 拒绝（见 _RESULT_DESC）。
            force_close = _as_bool(args.get("force_close", False))
            out = await manager.result(
                agent_id,
                max_chars=int(args.get("max_chars", 50000) or 50000),
                full=_as_bool(args.get("full", False)),
                close=_as_bool(args.get("close", False)),
                force_close=force_close,
            )
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _error(e)

    async def ga_send(args: dict) -> str:
        try:
            agent_id = _agent_id(args)
            if not agent_id:
                raise ValueError("agent_id 不能为空")
            out = await manager.send(agent_id, str(args.get("message", "") or ""))
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _error(e)

    async def ga_stop(args: dict) -> str:
        try:
            out = await manager.stop(_agent_id(args))
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _error(e)

    return [
        Tool(
            "ga_spawn",
            _SPAWN_DESC,
            {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "自包含任务书：目标、涉及路径、验收标准、禁止事项"
                                       "（GA 看不到当前对话，也不共享历史）",
                    },
                    "title": {"type": "string", "description": "可选；进度卡片上的短标题"},
                    "mode": {
                        "type": "string",
                        "enum": ["full_access", "read_only", "ask"],
                        "description": "可选；覆盖服务端默认权限模式",
                    },
                    "timeout_s": {
                        "type": "number",
                        "description": "可选；本次 prompt 的超时秒数，缺省用服务端配置（默认 1800）",
                    },
                },
                "required": ["task"],
            },
            ga_spawn,
        ),
        Tool(
            "ga_wait",
            _WAIT_DESC,
            {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "省略=等全部 GA 会话"},
                    "timeout_s": {
                        "type": "number",
                        "description": "默认 60；超时只表示还在跑，不会终止 GA",
                    },
                    "since_event_seq": {
                        "type": "integer",
                        "description": "可选；传上次 wait 返回的 last_seq 可只取增量事件",
                    },
                },
            },
            ga_wait,
        ),
        Tool(
            "ga_result",
            _RESULT_DESC,
            {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "max_chars": {
                        "type": "integer",
                        "description": "默认 50000；超限会截断并告知全文长度",
                    },
                    "full": {
                        "type": "boolean",
                        "description": "默认 false；true 取全量文本而不是最终答复",
                    },
                    "close": {
                        "type": "boolean",
                        "description": "默认 false；true 读完顺手释放打开会话名额。"
                                       "结果会被截断时会被拒绝（防没读完就销毁）",
                    },
                    "force_close": {
                        "type": "boolean",
                        "description": "默认 false；即使结果被截断也强制关闭。"
                                       "只在确定剩余内容可以丢弃时使用",
                    },
                },
                "required": ["agent_id"],
            },
            ga_result,
        ),
        Tool(
            "ga_send",
            _SEND_DESC,
            {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "message": {"type": "string", "description": "追加指令"},
                },
                "required": ["agent_id", "message"],
            },
            ga_send,
        ),
        Tool(
            "ga_stop",
            _STOP_DESC,
            {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "省略=停止全部 GA 会话"},
                },
            },
            ga_stop,
        ),
    ]
