"""Self-built tool abstraction (no langchain StructuredTool).

A Tool is: name + description + JSON-schema + async callable(args)->str.
ToolRegistry turns a list of Tools into OpenAI tool schemas and dispatches calls,
turning every exception into readable text (never raising), so the model can
self-correct on a bad argument (ReAct-style), aligning with GenericAgent's
_call_tool_safe behaviour.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    call: Callable[[dict], Awaitable[str]]
    # 工具来源，例如 "MCP: sqlbotmcp"。空字符串表示平台内置工具。
    #
    # 为什么必须带到模型面前：所有 MCP server 的工具会被拍平进同一个 list，
    # name 里没有命名空间。少了来源，模型无法回答「某个 server 的工具在不在」，
    # 只能按名字猜（2026-11-10 hx 会话：实际调用了 metric_search 仍声称
    # 「没有 sqlbot 工具」）。来源写进 description 是不改工具名契约的最小解。
    source: str = ""

    def describe_for_model(self) -> str:
        """模型看到的 description：带来源标签，便于判断工具归属。"""
        desc = self.description or self.name
        if not self.source:
            return desc
        tag = f"[{self.source}]"
        if desc.startswith(tag):
            return desc
        return f"{tag} {desc}"

    def to_openai_schema(self) -> dict:
        schema = self.input_schema or {"type": "object", "properties": {}}
        if not isinstance(schema, dict) or "type" not in schema:
            schema = {"type": "object", "properties": schema.get("properties", {}) if isinstance(schema, dict) else {}}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.describe_for_model(),
                "parameters": schema,
            },
        }


class ToolRegistry:
    def __init__(self, tools: Optional[list[Tool]] = None):
        self._tools: list[Tool] = list(tools or [])
        self._by_name = {t.name: t for t in self._tools}

    def add(self, tool: Tool) -> None:
        self._tools.append(tool)
        self._by_name[tool.name] = tool

    @property
    def tools(self) -> list[Tool]:
        return self._tools

    def schemas(self) -> list[dict]:
        return [t.to_openai_schema() for t in self._tools]

    def get(self, name: str) -> Optional[Tool]:
        return self._by_name.get(name)

    def names(self) -> list[str]:
        return list(self._by_name.keys())

    async def dispatch(self, name: str, args: Optional[dict]) -> str:
        """Run one tool; NEVER raise. Errors become readable text for the model."""
        tool = self._by_name.get(name)
        if tool is None:
            return f"[工具执行错误] 未知工具: {name}。可用工具: {self.names()}"
        if not isinstance(args, dict):
            args = {}
        try:
            out = await tool.call(args)
        except Exception as e:  # noqa: BLE001
            return _format_tool_error(name, e, tool.input_schema)
        if isinstance(out, str):
            return out
        try:
            return json.dumps(out, ensure_ascii=False, default=str)
        except Exception:
            return str(out)


def _schema_props(schema: Any) -> list[str]:
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if isinstance(props, dict):
        return list(props.keys())
    return []


def _format_tool_error(name: str, exc: Exception, schema: Any) -> str:
    msg = str(exc)
    low = msg.lower()
    hint = ""
    if ("validation" in low) or ("unexpected keyword" in low) or ("argument" in low) or ("required" in low):
        hint = (
            f"\n[参数校验失败] 请严格按工具参数定义调用，不要臆造参数名。"
            f"本次可用参数: {_schema_props(schema)}。"
        )
    return f"[工具执行错误] {type(exc).__name__}: {msg}{hint}"
