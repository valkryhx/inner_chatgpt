"""Server-side ownership scope shared by history, subagents and teams.

The scope is always constructed from authenticated/server-generated values. Tool
arguments and LLM output must never be able to provide or override it.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal


ROOT_AGENT_ID = "root"


@dataclass(frozen=True)
class AgentScope:
    user_id: str
    session_id: str
    message_id: str
    generation_id: str
    agent_id: str = ROOT_AGENT_ID
    team_id: str = ""
    role: Literal["root", "lead", "teammate"] = "root"

    def child(self, name: str, *, team_id: str = "",
              role: Literal["teammate"] = "teammate") -> "AgentScope":
        return replace(self, agent_id=f"sub:{name}", team_id=team_id or self.team_id, role=role)


@dataclass(frozen=True)
class TeamScope:
    user_id: str
    session_id: str
    generation_id: str
    team_id: str
    agent_id: str = ROOT_AGENT_ID
    role: Literal["lead", "teammate"] = "lead"

    @classmethod
    def from_agent(cls, scope: AgentScope) -> "TeamScope":
        if not scope.team_id:
            raise ValueError("team_id is required")
        return cls(
            user_id=scope.user_id,
            session_id=scope.session_id,
            generation_id=scope.generation_id,
            team_id=scope.team_id,
            agent_id=scope.agent_id,
            role="teammate" if scope.role == "teammate" else "lead",
        )

    def key(self) -> tuple[str, str, str, str]:
        return (self.user_id, self.session_id, self.generation_id, self.team_id)
