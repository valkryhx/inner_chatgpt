"""Tool exposure for Agent Team lead and teammate roles.

Identity is captured by these builders.  Tool arguments can only provide task
content, ids, action names and message text; user/session/generation/team/agent
always come from the injected :class:`TeamHost` scope.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from .team_host import TeamHost, TeamHostError
from .tools import Tool, ToolRegistry

LEAD_TOOL_NAMES = frozenset({
    "team_status", "task_create", "task_get", "task_list", "task_update",
    "send_message", "list_messages", "spawn_teammate",
})
TEAMMATE_TOOL_NAMES = frozenset({
    "task_get", "task_list", "task_update", "send_message", "list_messages",
})

SpawnFn = Callable[..., Awaitable[dict[str, Any]]]


def _err(msg: str) -> str:
    return f"[工具执行错误] {msg}"


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _build_tools(host: TeamHost, *, teammate: bool) -> list[Tool]:
    async def team_status(args: dict) -> str:
        return _json(await host.status())

    async def task_create(args: dict) -> str:
        blocked = args.get("blocked_by", args.get("blockedBy", []))
        if not isinstance(blocked, list):
            return _err("blocked_by 必须是任务 id 数组")
        task = await host.create_task(
            subject=str(args.get("subject", "")),
            description=str(args.get("description", "")),
            blocked_by=[str(x) for x in blocked if str(x)],
        )
        return _json(task)

    async def task_get(args: dict) -> str:
        task_id = str(args.get("task_id", "")).strip()
        if not task_id:
            return _err("task_id 不能为空")
        task = await host.get_task(task_id)
        if task is None:
            return _err("task not found")
        return _json(task)

    async def task_list(args: dict) -> str:
        return _json(await host.list_tasks(status=str(args.get("status", "") or "")))

    async def task_update(args: dict) -> str:
        task_id = str(args.get("task_id", "")).strip()
        if not task_id:
            return _err("task_id 不能为空")
        try:
            version = int(args.get("expected_version"))
        except (TypeError, ValueError):
            return _err("expected_version 必须是整数")
        out = await host.update_task(
            task_id, str(args.get("action", "")), expected_version=version
        )
        return _json(out)

    async def send_message(args: dict) -> str:
        return _json(await host.send_message(
            recipient=str(args.get("recipient", "")),
            content=str(args.get("content", "")),
            kind=str(args.get("kind", "note") or "note"),
        ))

    async def list_messages(args: dict) -> str:
        # The closure enforces teammate inbox isolation as a second boundary,
        # independently of the Host implementation.
        recipient = host.scope.agent_id if teammate else str(args.get("recipient", "") or "")
        return _json(await host.list_messages(
            recipient=recipient,
            unread_only=bool(args.get("unread_only", False)),
            limit=int(args.get("limit", 100) or 100),
        ))

    async def spawn_teammate(args: dict) -> str:
        task_id = str(args.get("team_task_id", "") or "")
        version = args.get("expected_version")
        if version is not None:
            try:
                version = int(version)
            except (TypeError, ValueError):
                return _err("expected_version 必须是整数")
        try:
            max_turns = int(args.get("max_turns", 0) or 0)
        except (TypeError, ValueError):
            return _err("max_turns 必须是整数")
        out = await host.spawn_teammate(
            task_name=str(args.get("task_name", "")),
            message=str(args.get("message", "")),
            team_task_id=task_id,
            expected_version=version,
            max_turns=max_turns,
        )
        return _json(out)

    tools = [
        Tool(
            "task_get", "读取当前团队的一个任务。", {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            }, task_get,
        ),
        Tool(
            "task_list", "列出当前团队任务。", {
                "type": "object",
                "properties": {"status": {"type": "string",
                    "enum": ["", "pending", "in_progress", "completed", "cancelled"]}},
            }, task_list,
        ),
        Tool(
            "task_update",
            "CAS 更新当前团队任务：claim（认领）/ complete（完成）/ reclaim（回收）。"
            "lead 用 spawn_teammate 绑定任务时已完成认领，teammate 直接按返回的 version complete；"
            "只有未绑定任务才 claim。必须传读取时的 version。",
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "action": {"type": "string", "enum": ["claim", "complete", "reclaim"]},
                    "expected_version": {"type": "integer"},
                },
                "required": ["task_id", "action", "expected_version"],
            }, task_update,
        ),
        Tool(
            "send_message",
            "给当前团队发送消息。lead 可发给 teammate；teammate 只能发给 lead/root。",
            {
                "type": "object",
                "properties": {
                    "recipient": {"type": "string"},
                    "content": {"type": "string"},
                    "kind": {"type": "string", "enum": ["note", "result", "request"]},
                },
                "required": ["recipient", "content"],
            }, send_message,
        ),
        Tool(
            "list_messages", "读取当前团队消息。teammate 默认只读发给自己的消息。", {
                "type": "object",
                "properties": {
                    "recipient": {"type": "string"},
                    "unread_only": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
            }, list_messages,
        ),
    ]
    if not teammate:
        tools = [
            Tool(
                "team_status", "读取当前团队状态、任务、事件和消息。", {
                    "type": "object", "properties": {},
                }, team_status,
            ),
            Tool(
                "task_create", "创建团队任务，可由本团队其他任务阻塞。", {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "description": {"type": "string"},
                        "blocked_by": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["subject"],
                }, task_create,
            ),
            *tools,
            Tool(
                "spawn_teammate",
                "创建 teammate 并可选绑定团队任务。spawn 失败会回收任务。",
                {
                    "type": "object",
                    "properties": {
                        "task_name": {"type": "string"},
                        "message": {"type": "string"},
                        "team_task_id": {"type": "string"},
                        "expected_version": {"type": "integer"},
                        "max_turns": {"type": "integer", "description": "可选，覆盖默认轮数上限（默认 300）"},
                    },
                    "required": ["task_name", "message"],
                }, spawn_teammate,
            ),
        ]
    return tools


def build_lead_tools(host: TeamHost, *, spawn_fn: SpawnFn | None = None) -> list[Tool]:
    """Build the full eight-tool lead surface."""
    if host.role != "lead":
        raise ValueError("build_lead_tools requires a lead TeamHost")
    if spawn_fn is not None:
        host._spawn_fn = spawn_fn
    return _build_tools(host, teammate=False)


def build_teammate_tools(host: TeamHost) -> list[Tool]:
    """Build the reduced teammate surface; management tools are absent."""
    if host.role != "teammate":
        raise ValueError("build_teammate_tools requires a teammate TeamHost")
    return _build_tools(host, teammate=True)


def build_team_tool_registry(host: TeamHost, *,
                             spawn_fn: SpawnFn | None = None) -> ToolRegistry:
    builder = build_lead_tools if host.role == "lead" else build_teammate_tools
    return ToolRegistry(builder(host, spawn_fn=spawn_fn) if host.role == "lead"
                        else builder(host))
