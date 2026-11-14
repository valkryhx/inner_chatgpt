export interface CompactionState {
  compacted: boolean
  summary_tokens: number
  kept_messages: number
  trigger?: string
}

export interface Session {
  id: string
  user_id: string
  title: string
  model_id: string
  /** 会话级思考等级覆盖；空字符串表示沿用模型配置 */
  reasoning_effort?: string
  provider: string
  created_at: number
  updated_at: number
  pinned: boolean
  archived: boolean
  compaction: CompactionState
}

export interface ToolBlock {
  type: 'tool'
  id: string
  name: string
  input?: unknown
  result?: string
}

export interface ChartSeries {
  name: string
  values: (number | null)[]
}

export interface ChartSpec {
  type?: 'chart'
  spec_version?: number
  chart_type: 'line' | 'bar' | 'pie' | 'table'
  title?: string | null
  option: Record<string, unknown>
  data: { categories: string[]; series: ChartSeries[] }
  meta?: { unit?: string | null; x_name?: string | null; y_name?: string | null }
}

export interface ChartBlock {
  type: 'chart'
  spec: ChartSpec
}

export interface TextBlock {
  type: 'text'
  text: string
}

export interface SummaryBlock {
  type: 'summary'
  text: string
  trigger?: string
  summary_tokens?: number
  kept_messages?: number
  dropped_messages?: number
}

export interface ReasoningBlock {
  type: 'reasoning'
  text: string
}

/** 平台注入到本轮上下文的消息（用户可见，便于解释行为变化）。
 *
 * 来源不止团队：还包括用户中途插话（steering）与子代理完成通知。
 * 前端按 text 的前缀区分来源并显示不同标签，见 MessageList 的 InjectedNote。
 */
export interface InjectedMessageBlock {
  type: 'injected_message'
  text: string
}

/** 子代理过程事件（卡片内时间线用） */
export interface SubagentEventItem {
  kind: 'text' | 'reasoning' | 'tool_start' | 'tool_result'
  id?: string
  name?: string
  text?: string
  input?: unknown
  result?: string
  is_error?: boolean
}

/** 子代理卡片：一次 spawn 对应一张卡 */
export interface SubagentBlock {
  type: 'subagent'
  agent_id: string
  name: string
  role: string
  status: 'running' | 'stopping' | 'completed' | 'errored' | 'stopped' | 'closed'
  /** 卡片出现时的本地时间戳，用于显示已运行时长 */
  startedAt?: number
  /** 进入终态的时间戳；结束后时长固定在 (endedAt - startedAt) */
  endedAt?: number
  task: string
  events: SubagentEventItem[]
  result?: string
  turns?: number
  error?: string
}

/** Agent Team durable task/message projections. */
export type TeamStatus = 'active' | 'completed' | 'stopped' | 'failed' | 'stale'
export type TeamTaskStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled'

export interface TeamRun {
  team_id: string
  user_id: string
  session_id: string
  generation_id: string
  lead_agent_id: string
  name: string
  status: TeamStatus
  created_at: number
  updated_at: number
  closed_at: number
}

export interface TeamTask {
  task_id: string
  team_id: string
  user_id: string
  session_id: string
  generation_id: string
  subject: string
  description: string
  status: TeamTaskStatus
  owner_agent_id: string
  created_by: string
  created_at: number
  updated_at: number
  version: number
  blockedBy: string[]
}

export interface TeamMessage {
  message_id: string
  team_id: string
  user_id?: string
  session_id?: string
  generation_id?: string
  sender_agent_id: string
  recipient_agent_id: string
  kind: string
  content: string
  created_at: number
  delivered_at?: number
  read_at?: number
}

export interface TeamEvent {
  type: string
  team_id: string
  session_id?: string
  generation_id?: string
  agent_id?: string
  task_id?: string
  event_seq: number
  payload: Record<string, unknown>
  status?: string
  updated_at?: number
}

export interface TeamSnapshot {
  team: TeamRun | null
  tasks: TeamTask[]
  events: TeamEvent[]
  messages: TeamMessage[]
}

export interface Attachment {
  path: string
  name?: string
  mime?: string
  size?: number
}

/** 上传成功后的返回值：多一个可直接渲染的鉴权读取地址。 */
export interface UploadedImage extends Attachment {
  url: string
}

export interface AttachmentsBlock {
  type: 'attachments'
  items: Attachment[]
}

export type MessageBlock =
  | AttachmentsBlock
  | TextBlock
  | ToolBlock
  | SummaryBlock
  | ChartBlock
  | ReasoningBlock
  | SubagentBlock
  | InjectedMessageBlock

export interface Message {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  reasoning: string
  blocks: MessageBlock[]
  status: 'streaming' | 'complete' | 'error' | 'stopped' | 'queued'
  created_at: number
  /** 本轮开始时间（前端本地计时，仅当前页面生命周期有效）。 */
  run_started_at?: number
  /** 本轮结束时间（前端本地计时，仅当前页面生命周期有效）。 */
  run_finished_at?: number
}

/** 当前会话的上下文占用（与后端自动压缩使用同一 token 估算口径）。 */
export interface ContextUsage {
  used_tokens: number
  context_window: number
  auto_compact_tokens: number
  hard_limit_tokens: number
  percent: number
  compact_percent: number
  remaining_to_compact: number
  compacted: boolean
}

export interface SessionDetail {
  session: Session
  messages: Message[]
}

/** 一条正文搜索命中：所属会话 + 上下文片段 + 片段内命中位置。 */
export interface MessageSearchHit {
  message_id: string
  session_id: string
  session_title: string
  role: 'user' | 'assistant' | 'system'
  snippet: string
  /** 片段内命中起点；-1 表示片段中不含原样关键词，不高亮。 */
  match_offset: number
  match_length: number
  created_at: number
  session_updated_at: number
}

export interface MessageSearchResult {
  query: string
  hits: MessageSearchHit[]
  has_more: boolean
  elapsed_ms: number
}

/** stop 接口返回的已落库 assistant 快照，用于避免 abort 后历史覆盖本地内容。 */
export interface StopResult {
  ok: boolean
  stopped: number
  subagents_stopped: number
  message?: Message | null
}

// 流式事件
export type StreamEvent =
  | { type: 'message_start'; message_id: string }
  | { type: 'text_delta'; text: string }
  | { type: 'thinking_delta'; text: string }
  | { type: 'tool_start'; id: string; name: string; input?: unknown }
  | { type: 'tool_input'; id: string; partial_json: string }
  | { type: 'tool_result'; id: string; name: string; content: string; is_error?: boolean }
  | { type: 'chart'; spec: ChartSpec }
  | { type: 'subagent_start'; agent_id: string; name: string; role: string; task: string }
  | { type: 'subagent_event'; agent_id: string; event: StreamEvent }
  | { type: 'subagent_end'; agent_id: string; name?: string; status: string; result?: string; turns?: number; error?: string }
  | { type: 'subagent_status'; agent_id: string; name?: string; status: string }
  | { type: 'compaction'; summary_tokens: number; kept_messages: number; trigger?: string }
  | { type: 'reasoning_effort'; effective: string; source: 'session' | 'model_config' | string; model: string }
  | { type: 'team_event'; event: TeamEvent }
  | { type: 'message_injected'; text: string }
  | { type: 'llm_retry'; attempt: number; limit: number; error: string }
  | { type: 'team_wait'; reason: string; grace_left: number }
  | { type: 'done'; message_id: string; status: 'complete' | 'stopped' | 'error' }
  | { type: 'error'; error: string }


export interface User {
  id: string
  email: string
  username: string
  name: string
  role: string
}

export interface AuthResponse {
  user: User
  access_token: string
  expires_at: number
}

export interface LlmModelOption {
  id: string
  label: string
  model: string
  wire_api: string
  /** 模型配置里的 reasoning_effort；空字符串表示使用上游默认值。 */
  reasoning_effort: string
  is_default: boolean
  source: string
}

export interface LLMConfig {
  id: string
  label: string
  wire_api: string
  base_url: string
  api_key_masked: string
  has_api_key: boolean
  model: string
  context_window: number
  max_tokens: number
  auto_compact_tokens?: number | null
  hard_limit_tokens?: number | null
  reasoning_effort: string
  /** 单次模型调用的读超时（秒）。语义是「两次读取间的最大空闲」，不是总时长。 */
  read_timeout?: number
  is_default: boolean
}

export interface McpServerStatus {
  name: string
  ok: boolean
  tool_count: number
  error: string
  tools: string[]
}


export interface MCPServer {
  id: string
  name: string
  transport: string
  command: string
  args: string[]
  url: string
  headers: Record<string, string>
  env: Record<string, string>
  enabled: boolean
}


export interface Skill {
  name: string
  description: string
  source: 'builtin' | 'shared' | 'user' | string
  chars: number
  updated_at: number
  model_invocable: boolean
  /** 文件数：>1 表示该 Skill 带参考资料/脚本等附属文件 */
  files?: number
  /** 内置/公用 Skill 只读 */
  readonly?: boolean
}

/* ---------- 记忆（设置页「我的记忆」） ---------- */

export type MemoryKind = 'user' | 'feedback' | 'project' | 'reference'

export interface MemoryEntry {
  slug: string
  title: string
  description: string
  kind: MemoryKind | string
  tags: string[]
  scope: string
  created_at: number
  updated_at: number
  /** 内容 sha256 前 12 位，用于 CAS（改前带上可避免覆盖别人的修改） */
  version: string
  chars: number
}

export interface MemoryPage {
  entries: MemoryEntry[]
  /** 还有多少条没取到（配合 next_cursor 续读） */
  remaining: number
  next_cursor: string
  total: number
}

export interface MemoryDetail extends MemoryEntry {
  content: string
}

export interface DreamState {
  enabled: boolean
  /** 上次**成功**合并的时刻（ms）；0 = 从没跑过 */
  last_consolidated?: number
  /** 上次**开始**的时刻（ms），用于诊断「反复在同一处崩」 */
  last_attempt?: number
  /** idle | running | failed | interrupted */
  last_status?: string
  last_error?: string
  runs?: number
  /** Phase 3 成功了但索引还没重建；下一轮巡检会补上 */
  index_pending?: boolean
  min_hours?: number
  min_sessions?: number
}

export interface MemoryStatus {
  enabled: boolean
  /** 自动提取开关：默认 false。开着才会「自己长东西」，需要用户知情 */
  auto_write: boolean
  dream_enabled: boolean
  disabled_by_env: boolean
  total: number
  inject_budget: number
  inject_max: number
  dream?: DreamState
}

export interface DreamRunResult {
  ran: boolean
  reason: string
  user_id: string
  written: string[]
  deleted: string[]
  index_rebuilt: boolean
  error: string
  elapsed_s: number
}
 
/* ---------- workspace 文件下载 ---------- */

export interface WorkspaceEntry {
  name: string
  /** workspace 相对路径（POSIX） */
  path: string
  type: 'file' | 'dir'
  size: number
  mtime: number
  /** 以 . 开头的隐藏项；`.subagents` 为子代理产物 */
  hidden?: boolean
}

export interface WorkspaceListing {
  root: string
  entries: WorkspaceEntry[]
  truncated: boolean
  limit: number
}


/* ---------- A2A per-user access ---------- */

export interface A2AAgentInfo {
  agent_id: string
  url: string
  card_url: string
  tenant: string
  /** 用户级开关：关着时卡片与 JSON-RPC 一律 404 */
  enabled: boolean
  /** 实例级开关（由 INNER_A2A_ENABLED 决定，前端不可改） */
  instance_enabled?: boolean
  /** full / read_only / chat_only，附人话说明 */
  tool_policies?: { id: string; description: string }[]
}

export interface A2AToken {
  id: string
  user_id: string
  agent_id: string
  name: string
  prefix: string
  scopes: string[]
  /** 建 token 时定死的能力档位，运行期不再询问 */
  tool_policy?: string
  created_at: number
  last_used_at?: number | null
  expires_at?: number | null
  revoked_at?: number | null
}

export interface A2ATokenCreated extends A2AToken {
  token: string
  url: string
  card_url: string
  warning: string
}
