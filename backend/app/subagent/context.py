"""AgentContext：把「归属身份」显式贯穿到每一次子代理调用。

抄 Aivory 的 RunOpts{UserID,ConversationID,MessageID,WorkspaceID} 思路：
任何写文件 / 写库 / 调模型 / 读技能，都必须从这里取 user_id，
不允许从环境变量或模块级全局变量取。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

ROOT_AGENT_ID = "root"


@dataclass(frozen=True)
class AgentContext:
    user_id: str
    session_id: str
    message_id: str
    generation_id: str
    agent_id: str = ROOT_AGENT_ID
    parent_agent_id: str | None = None
    depth: int = 0

    @property
    def is_subagent(self) -> bool:
        return self.agent_id != ROOT_AGENT_ID

    def child(self, name: str) -> "AgentContext":
        return replace(
            self,
            agent_id=f"sub:{name}",
            parent_agent_id=self.agent_id,
            depth=self.depth + 1,
        )

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "generation_id": self.generation_id,
            "agent_id": self.agent_id,
            "parent_agent_id": self.parent_agent_id,
            "depth": self.depth,
        }
