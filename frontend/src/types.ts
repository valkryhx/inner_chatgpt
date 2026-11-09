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
  status: 'running' | 'completed' | 'errored' | 'stopped' | 'closed'
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

export type MessageBlock =
  | TextBlock
  | ToolBlock
  | SummaryBlock
  | ChartBlock
  | ReasoningBlock
  | SubagentBlock

export interface Message {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  reasoning: string
  blocks: MessageBlock[]
  status: 'streaming' | 'complete' | 'error' | 'stopped'
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
  | { type: 'compaction'; summary_tokens: number; kept_messages: number; trigger?: string }
  | { type: 'team_event'; event: TeamEvent }
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
  is_default: boolean
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
}

export interface A2AToken {
  id: string
  user_id: string
  agent_id: string
  name: string
  prefix: string
  scopes: string[]
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
