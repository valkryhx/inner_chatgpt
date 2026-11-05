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
}

export interface SessionDetail {
  session: Session
  messages: Message[]
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
  | { type: 'done'; message_id: string; status: string }
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
