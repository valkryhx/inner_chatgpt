"""Pydantic 请求/响应模型（字段命名对齐 Aivory 风格，snake_case）。"""
from __future__ import annotations

from typing import Any, Literal, Optional

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
    # 会话级思考等级覆盖；空字符串 = 沿用模型配置。前端下拉要读它。
    reasoning_effort: str = ""


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


class MessageSearchHit(BaseModel):
    """一条正文命中：所属会话 + 上下文片段 + 片段内命中位置。"""
    message_id: str
    session_id: str
    session_title: str = ""
    role: str = ""
    snippet: str = ""
    # 片段内命中起点；-1 表示片段中不含原样关键词（大小写/裁切导致），前端不高亮。
    match_offset: int = -1
    match_length: int = 0
    created_at: int = 0
    session_updated_at: int = 0


class MessageSearchOut(BaseModel):
    query: str = ""
    hits: list[MessageSearchHit] = Field(default_factory=list)
    has_more: bool = False
    # 服务端耗时（毫秒），便于判断是否需要上 FTS5。
    elapsed_ms: float = 0.0


class ContextUsageOut(BaseModel):
    used_tokens: int = 0
    context_window: int = 0
    auto_compact_tokens: int = 0
    hard_limit_tokens: int = 0
    percent: float = 0.0
    compact_percent: float = 0.0
    remaining_to_compact: int = 0
    compacted: bool = False


class CreateSessionIn(BaseModel):
    title: Optional[str] = None
    model_id: Optional[str] = None


class UpdateSessionIn(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None
    model_id: Optional[str] = None
    # 会话级思考等级覆盖；空字符串 = 清空覆盖（沿用模型配置）。合法值见 app/reasoning.py
    reasoning_effort: Optional[str] = None


class AttachmentIn(BaseModel):
    path: str
    name: Optional[str] = None
    mime: Optional[str] = None


class SendMessageIn(BaseModel):
    text: str = ""
    generation_id: Optional[str] = None
    # 可选：随消息发送的图片引用（必须先通过 POST /api/me/uploads 落盘）。
    attachments: list[AttachmentIn] = []
    # 可选：本次消息临时覆盖思考等级（不改会话设置）。空=不覆盖。
    reasoning_effort: Optional[str] = None
    # 发送方式：
    #   "send"  —— 常规发送，开一个新回合（默认，历史行为）
    #   "queue" —— 当前有回合在跑时，把这条消息插进那个回合（steering）
    # 语义见 docs/20261111-用户中途插话steering实施方案.md
    mode: Literal["send", "queue"] = "send"


class StopIn(BaseModel):
    message_id: Optional[str] = None
    generation_id: Optional[str] = None


class StopResult(BaseModel):
    ok: bool = True
    stopped: int = 0
    subagents_stopped: int = 0
    message: Optional[MessageOut] = None


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


# ---------------- 模型列表探测（查端点自己的 /models） ----------------
class ProbeModelsIn(BaseModel):
    base_url: str = ""
    # 新建配置时用户还没保存，key 只在这次请求里用一下，不落库。
    api_key: str = ""
    # 编辑既有配置时可只给 config_id，服务端用库里存的 key。
    config_id: str = ""


class RemoteModelItem(BaseModel):
    id: str
    owned_by: str = ""


class ProbeModelsOut(BaseModel):
    ok: bool = False
    models: list[RemoteModelItem] = Field(default_factory=list)
    error: str = ""
    hint: str = ""
    endpoint: str = ""


class ProbeReasoningIn(BaseModel):
    """探测某个模型是否接受 reasoning_effort。

    与 ProbeModelsIn 同构：新建配置时 key 只在本次请求用，编辑时可用 config_id。
    """
    base_url: str = ""
    api_key: str = ""
    config_id: str = ""
    model: str = ""
    # 空 = 未指定；有 config_id 时用库里存的那条。显式传值优先。
    wire_api: str = ""


class ProbeReasoningOut(BaseModel):
    # None = 无法判定（网络/鉴权/模型名错误），与 False（明确被忽略）必须区分。
    supported: Optional[bool] = None
    # 端点自报的可选档位（从 400/422 的 "expected one of ..." 里解析）；可能为空。
    levels: list[str] = Field(default_factory=list)
    # 本次探测实际发出去的非法档位，便于复现。
    checked_level: str = ""
    error: str = ""
    hint: str = ""


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
