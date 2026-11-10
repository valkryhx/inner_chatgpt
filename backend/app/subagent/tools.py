"""5 个编排工具（spawn / wait / read_agent_result / list_agents / close_agent）。

只有 root agent 的 registry 里会注入这些工具；子代理的 registry 是裁剪过的，
根本不存在它们（能力闭世界，见 capabilities.py）。
"""
from __future__ import annotations

import json

from ..tools import Tool
from .host import SubagentHost
from .registry import SubagentError


def _err(msg: str) -> str:
    return f"[工具执行错误] {msg}"


def build_subagent_tools(host: SubagentHost) -> list[Tool]:
    async def spawn_agent(args: dict) -> str:
        try:
            out = await host.spawn(
                task_name=str(args.get("task_name", "")),
                message=str(args.get("message", "")),
                fork_turns=str(args.get("fork_turns", "none") or "none"),
                background=bool(args.get("background", True)),
                max_turns=int(args.get("max_turns", 0) or 0),
            )
        except SubagentError as e:
            return _err(str(e))
        return json.dumps(out, ensure_ascii=False)

    async def wait_agent(args: dict) -> str:
        targets = args.get("targets")
        if not isinstance(targets, list):
            single = args.get("target")
            targets = [single] if single else None
        try:
            out = await host.wait(
                targets=targets,
                condition=str(args.get("condition", "all_terminal") or "all_terminal"),
                timeout_s=float(args.get("timeout_seconds", 60) or 60),
                since_event_seq=int(args.get("since_event_seq", 0) or 0),
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"{type(e).__name__}: {e}")
        return json.dumps(out, ensure_ascii=False)

    async def read_agent_result(args: dict) -> str:
        target = str(args.get("target", ""))
        if not target:
            return _err("target 不能为空")
        out = await host.read_result(target, max_chars=int(args.get("max_output_chars", 20000) or 20000))
        if not out.get("ok"):
            return _err(str(out.get("error") or "读取失败"))
        header = (f"[子代理结果] {out['name']} status={out['status']} "
                  f"turns={out['turns']}" + (f" error={out['error']}" if out.get("error") else ""))
        if out.get("truncated"):
            header += f"（已截断，共 {out['chars']} 字符）"
        return header + "\n\n" + out["result"]

    async def list_agents(args: dict) -> str:
        rows = host.list_agents(include_closed=bool(args.get("include_closed", False)))
        if not rows:
            return "当前没有子代理。"
        return json.dumps(rows, ensure_ascii=False, indent=2)

    async def close_agent(args: dict) -> str:
        target = str(args.get("target", ""))
        if not target:
            return _err("target 不能为空")
        out = await host.close(target, cascade=bool(args.get("cascade", False)))
        if not out.get("ok"):
            return _err(str(out.get("error") or "关闭失败"))
        return json.dumps({"ok": True, "closed": out.get("closed", []),
                           "note": "已释放槽位"}, ensure_ascii=False)

    async def followup_task(args: dict) -> str:
        out = await host.followup(
            str(args.get("target", "") or ""),
            str(args.get("message", "") or ""),
        )
        return json.dumps(out, ensure_ascii=False)

    return [
        Tool(
            "spawn_agent",
            "派生一个子代理执行自包含的旁路任务。子代理看不到本对话：必须把背景、路径、约束、"
            "期望输出和「什么算完成」写进 message。子代理拥有与父代理相同的业务工具（文件读写、"
            "code_run、Skill、MCP），但不能调用编排工具或再派生子代理。"
            "派活后需 wait_agent 等待，再 read_agent_result 取结果。",
            {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string", "description": "小写字母/数字/下划线，1-32 字符，本会话唯一"},
                    "message": {"type": "string", "description": "自包含任务说明（背景/路径/约束/期望输出/完成标准）"},
                    "fork_turns": {"type": "string", "description": "none(默认,不继承对话) | all | recent:N"},
                    "background": {"type": "boolean", "description": "默认 true"},
                    "max_turns": {"type": "integer", "description": "可选，覆盖默认轮数上限"},
                },
                "required": ["task_name", "message"],
            },
            spawn_agent,
        ),
        Tool(
            "wait_agent",
            "等待子代理。超时不算失败（子代理仍在跑），先看 remaining_targets 再继续 wait；"
            "已有结果用 read_agent_result。",
            {
                "type": "object",
                "properties": {
                    "targets": {"type": "array", "items": {"type": "string"},
                                "description": "task_name 列表；省略=所有存活子代理"},
                    "condition": {"type": "string", "enum": ["event", "all_terminal", "result_available"],
                                  "description": "默认 all_terminal"},
                    "timeout_seconds": {"type": "number", "description": "默认 60"},
                    "since_event_seq": {"type": "integer",
                                        "description": "可选；传上一次 wait 返回的 event_seq 可只取增量事件"},
                },
            },
            wait_agent,
        ),
        Tool(
            "read_agent_result",
            "读取子代理的权威最终结果（不是 stdout）。子代理必须已结束。",
            {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "max_output_chars": {"type": "integer", "description": "默认 20000"},
                },
                "required": ["target"],
            },
            read_agent_result,
        ),
        Tool(
            "list_agents",
            "列出本会话的子代理花名册（名字/角色/状态/轮数）。",
            {
                "type": "object",
                "properties": {"include_closed": {"type": "boolean", "description": "是否包含已关闭"}},
            },
            list_agents,
        ),
        Tool(
            "followup_task",
            "向仍在运行的子代理追加一条指令（steer）。不会打断它当前的工具调用，"
            "而是在它下一次模型调用前作为新的用户消息注入。子代理已结束时会被拒绝——"
            "那种情况用 read_agent_result 取结果，需要继续工作就重新 spawn_agent。",
            {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "子代理 task_name"},
                    "message": {"type": "string", "description": "追加指令（自包含、可直接执行）"},
                },
                "required": ["target", "message"],
            },
            followup_task,
        ),
        Tool(
            "close_agent",
            "关闭子代理并释放并发槽位。它自己派生的子代理用 cascade=true 一并关闭。",
            {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "cascade": {"type": "boolean", "description": "是否连带关闭其子代"},
                    "reason": {"type": "string"},
                },
                "required": ["target"],
            },
            close_agent,
        ),
    ]
