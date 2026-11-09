"""A2A v1.0 AgentCard for one per-user agent endpoint."""
from __future__ import annotations

from typing import Any


def build_agent_card(agent_id: str, base_url: str) -> dict[str, Any]:
    base = base_url.rstrip("/")
    url = f"{base}/a2a/{agent_id}"
    return {
        "name": "Hlaudz Agent",
        "description": "A self-hosted agent that can chat, use tools, and delegate to GenericAgent via ACP.",
        "supportedInterfaces": [
            {
                "url": url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
                "tenant": agent_id,
            }
        ],
        "version": "1.0.0",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "general-agent",
                "name": "General Agent",
                "description": "Chat, coding, file and tool tasks in the user's workspace.",
                "tags": ["chat", "coding", "tools"],
            }
        ],
        "securitySchemes": {
            "bearer": {
                "httpAuthSecurityScheme": {
                    "scheme": "bearer",
                    "bearerFormat": "opaque",
                }
            }
        },
        "securityRequirements": [{"schemes": {"bearer": {"list": []}}}],
    }
