"""FastMCP 示例工具服务。

MVP 演示用，提供几个安全、无副作用的工具：
- current_time: 返回当前时间
- calculator: 安全四则运算
- echo: 回显
- kv_set / kv_get: 演示有状态工具（进程内字典）

既可作为独立进程运行（`python -m mcp_server.server` 走 stdio/http），
也可被后端以 in-process 方式直接引用 `mcp` 实例。
"""
from __future__ import annotations

import ast
import operator
from datetime import datetime, timezone

from fastmcp import FastMCP

mcp = FastMCP("demo-tools")

_KV: dict[str, str] = {}

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("unsupported expression")


@mcp.tool
def current_time(timezone_name: str = "Asia/Shanghai") -> str:
    """返回当前日期时间。timezone_name 为 IANA 时区名（默认 Asia/Shanghai）。"""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone_name)
        return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@mcp.tool
def calculator(expression: str) -> str:
    """计算一个数学表达式（仅支持 + - * / % ** // 和括号、数字）。例如 "12*(3+4)"。"""
    try:
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval(tree)
        return str(result)
    except Exception as e:
        return f"计算失败: {e}"


@mcp.tool
def echo(text: str) -> str:
    """原样回显输入文本。"""
    return text


@mcp.tool
def kv_set(key: str, value: str) -> str:
    """把 value 存到键 key（进程内演示存储）。"""
    _KV[key] = value
    return f"已保存 {key}"


@mcp.tool
def kv_get(key: str) -> str:
    """读取键 key 的值（进程内演示存储）。"""
    return _KV.get(key, f"(不存在) {key}")


if __name__ == "__main__":
    mcp.run()
