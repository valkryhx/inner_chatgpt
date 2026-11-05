"""Pydantic 请求/响应模型（字段命名对齐 Aivory 风格，snake_case）。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class CompactionState(BaseModel):
    compacted: bool = False
    summary_tokens: int = 0
    kept_messages: int = 0


class SessionOut(BaseModel):
    id: str
    user_id: str = "local"
    title: str
    model_id: str = ""
    provider: str = ""
    created_at: int
    updated_at: int
    pinned: bool = False
    archived: bool = False
    compaction: CompactionState = Field(default_factory=CompactionState)


class MessageOut(BaseModel):
    id: str
    session_id: str
    role: str
    content: str = ""
    reasoning: str = ""
    blocks: list[Any] = Field(default_factory=list)
    status: str = "complete"
    created_at: int


class SessionListOut(BaseModel):
    sessions: list[SessionOut]
    has_more: bool = False


class SessionDetailOut(BaseModel):
    session: SessionOut
    messages: list[MessageOut]


class CreateSessionIn(BaseModel):
    title: Optional[str] = None
    model_id: Optional[str] = None


class UpdateSessionIn(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None
    model_id: Optional[str] = None


class SendMessageIn(BaseModel):
    text: str
    generation_id: Optional[str] = None


class StopIn(BaseModel):
    message_id: Optional[str] = None
    generation_id: Optional[str] = None


class CompactResult(BaseModel):
    compacted: bool
    trigger: str = "manual"
    dropped_messages: int = 0
    kept_messages: int = 0
    summary_tokens: int = 0
    before_messages: int = 0
    after_messages: int = 0
    message: str = ""


class HealthOut(BaseModel):
    ok: bool = True
    model: str = ""
    mcp_tools: int = 0


# ---------------- 多用户 / 认证 ----------------
class RegisterIn(BaseModel):
    email: str
    password: str
    name: Optional[str] = None
    username: Optional[str] = None


class LoginIn(BaseModel):
    # 兼容旧客户端：既接受 email 字段，也接受 username / identifier。
    email: str = ""
    username: str = ""
    identifier: str = ""
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    username: str = ""
    name: str = ""
    role: str = "user"


class AuthResponse(BaseModel):
    user: UserOut
    access_token: str
    expires_at: int


# ---------------- 用户级 LLM 配置 ----------------
class LLMConfigIn(BaseModel):
    label: str = ""
    wire_api: str = "openai_responses"   # openai_responses | openai_chat
    base_url: str
    api_key: str = ""
    model: str
    context_window: int = 128000
    max_tokens: int = 8192
    auto_compact_tokens: Optional[int] = None
    hard_limit_tokens: Optional[int] = None
    reasoning_effort: str = ""
    is_default: bool = False


class LLMConfigUpdate(BaseModel):
    label: Optional[str] = None
    wire_api: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    context_window: Optional[int] = None
    max_tokens: Optional[int] = None
    auto_compact_tokens: Optional[int] = None
    hard_limit_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None
    is_default: Optional[bool] = None


class LLMConfigOut(BaseModel):
    id: str
    label: str = ""
    wire_api: str = ""
    base_url: str = ""
    api_key_masked: str = ""
    has_api_key: bool = False
    model: str = ""
    context_window: int = 128000
    max_tokens: int = 8192
    auto_compact_tokens: Optional[int] = None
    hard_limit_tokens: Optional[int] = None
    reasoning_effort: str = ""
    is_default: bool = False


# ---------------- 用户级 MCP ----------------
class MCPServerIn(BaseModel):
    name: str = ""
    transport: str = "http"   # builtin_demo | http | sse | stdio
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class MCPServerUpdate(BaseModel):
    name: Optional[str] = None
    transport: Optional[str] = None
    command: Optional[str] = None
    args: Optional[list[str]] = None
    url: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    env: Optional[dict[str, str]] = None
    enabled: Optional[bool] = None


class MCPServerOut(BaseModel):
    id: str
    name: str = ""
    transport: str = "http"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


# ---------------- 标准 mcp.json（mcpServers）批量配置 ----------------
class MCPJsonIn(BaseModel):
    """用户在配置框里直接粘贴标准 mcp.json 内容（含 mcpServers 对象）。"""
    raw: str
    enabled: bool = True


class MCPImportResult(BaseModel):
    ok: bool = True
    added: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    total_servers: int = 0
    message: str = ""


# ---------------- 用户级 Skills ----------------
class SkillIn(BaseModel):
    name: str
    description: str = ""
    content: str = ""          # SKILL.md 正文（不含 frontmatter）
    document: Optional[str] = None  # 可选：完整 SKILL.md（含 frontmatter），优先于 content


class SkillUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    document: Optional[str] = None


class SkillOut(BaseModel):
    name: str
    description: str = ""
    source: str = "user"       # builtin | shared | user
    chars: int = 0
    updated_at: int = 0
    model_invocable: bool = True
    # Number of files in the skill directory (SKILL.md + bundled resources).
    # >1 means the skill ships extra reference/script files under ${SKILL_DIR}.
    files: int = 0
    readonly: bool = False
