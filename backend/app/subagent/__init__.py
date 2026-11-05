"""进程内 asyncio subagent 运行时（P2）。

设计见 ``docs/20261105-Subagent调研与设计方案.md``。
本包不 import langchain / langgraph，也不 import GenericAgent 包。
"""
from .context import AgentContext

__all__ = ["AgentContext"]
