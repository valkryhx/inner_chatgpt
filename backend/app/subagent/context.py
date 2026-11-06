"""AgentContext：把「归属身份」显式贯穿到每一次子代理调用。

抄 Aivory 的 RunOpts{UserID,ConversationID,MessageID,WorkspaceID} 思路：
任何写文件 / 写库 / 调模型 / 读技能，都必须从这里取 user_id，
不允许从环境变量或模块级全局变量取。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from ..scope import AgentScope

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
    team_id: str = ""
    role: Literal["root", "lead", "teammate"] = "root"

    @property
    def is_subagent(self) -> bool:
        return self.agent_id != ROOT_AGENT_ID

    def scope(self) -> AgentScope:
        return AgentScope(
            user_id=self.user_id,
            session_id=self.session_id,
            message_id=self.message_id,
            generation_id=self.generation_id,
            agent_id=self.agent_id,
            team_id=self.team_id,
            role=self.role,
        )

    def with_team(self, team_id: str,
                  *, role: Literal["lead", "teammate"] = "lead") -> "AgentContext":
        """Return a server-derived context bound to a durable team."""
        return replace(self, team_id=team_id, role=role)

    def child(self, name: str, *, team_id: str = "", role: Literal["teammate"] = "teammate") -> "AgentContext":
        return replace(
            self,
            agent_id=f"sub:{name}",
            parent_agent_id=self.agent_id,
            depth=self.depth + 1,
            team_id=team_id or self.team_id,
            role=role if (team_id or self.team_id) else self.role,
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
            "team_id": self.team_id,
            "role": self.role,
        }
