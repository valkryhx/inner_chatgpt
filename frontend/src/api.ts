import type {
  A2AAgentInfo,
  A2AToken,
  A2ATokenCreated,
  Attachment,
  AuthResponse,
  ContextUsage,
  LLMConfig,
  LlmModelOption,
  MCPServer,
  Session,
  SessionDetail,
  Message,
  UploadedImage,
  StopResult,
  StreamEvent,
  User,
  Skill,
  TeamSnapshot,
  WorkspaceListing,
} from './types'

const BASE = '/api'
const TOKEN_KEY = 'app_token'
const LEGACY_TOKEN_KEY = 'langgraph_token'

export function getToken(): string {
  const t = localStorage.getItem(TOKEN_KEY) || localStorage.getItem(LEGACY_TOKEN_KEY) || ''
  return t
}
export function setToken(t: string) {
  if (t) localStorage.setItem(TOKEN_KEY, t)
  else { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(LEGACY_TOKEN_KEY) }
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function detailText(value: unknown, fallback: string): string {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    const parts = value.map((item) => detailText(item, '')).filter(Boolean)
    return parts.length > 0 ? parts.join('；') : fallback
  }
  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>
    const parts = [obj.error, obj.message, obj.hint, obj.detail]
      .map((item) => detailText(item, ''))
      .filter(Boolean)
    if (parts.length > 0) return parts.join('：')
    try {
      return JSON.stringify(value)
    } catch {
      return fallback
    }
  }
  if (value === undefined || value === null || value === '') return fallback
  return String(value)
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...(extra || {}) }
  const t = getToken()
  if (t) h['authorization'] = 'Bearer ' + t
  return h
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    ...init,
    headers: authHeaders(init?.headers as Record<string, string> | undefined),
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body: unknown = await res.json()
      const obj = body && typeof body === 'object' ? body as Record<string, unknown> : {}
      detail = detailText(obj.detail ?? obj.error, detail)
    } catch {
      /* ignore */
    }
    if (res.status === 401 && !path.startsWith('/auth/')) {
      setToken('')
      window.dispatchEvent(new Event('auth:logout'))
    }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return undefined as unknown as T
  return res.json() as Promise<T>
}

export const api = {
  health: () => request<{ ok: boolean; model: string; mcp_tools: number }>('/health'),

  // auth
  register: (email: string, password: string, name?: string, username?: string) =>
    request<AuthResponse>('/auth/register', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ email, password, name, username }),
    }),
  login: (identifier: string, password: string) =>
    request<AuthResponse>('/auth/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ identifier, password }),
    }),
  logout: () => request<{ ok: boolean }>('/auth/logout', { method: 'POST' }),
  me: () => request<User>('/auth/me'),

  // sessions
  listSessions: (limit = 100, offset = 0) =>
    request<{ sessions: Session[]; has_more: boolean }>(`/sessions?limit=${limit}&offset=${offset}`),
  createSession: (title?: string, modelId?: string) =>
    request<Session>('/sessions', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ title, model_id: modelId ?? '' }),
    }),
  getSession: (id: string) => request<SessionDetail>(`/sessions/${id}`),
  getContextUsage: (id: string) => request<ContextUsage>(`/sessions/${id}/context`),
  renameSession: (id: string, title: string) =>
    request<Session>(`/sessions/${id}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ title }),
    }),
  setSessionModel: (id: string, modelId: string) =>
    request<Session>(`/sessions/${id}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ model_id: modelId }),
    }),
  setSessionReasoningEffort: (id: string, effort: string) =>
    request<Session>(`/sessions/${id}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ reasoning_effort: effort }),
    }),
  deleteSession: (id: string) => request<{ ok: boolean }>(`/sessions/${id}`, { method: 'DELETE' }),
  compact: (id: string, opts?: { instructions?: string; keep?: number }) =>
    request<{
      compacted: boolean
      trigger: string
      dropped_messages: number
      kept_messages: number
      summary_tokens: number
      before_messages: number
      after_messages: number
      message: string
    }>(`/sessions/${id}/compact`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(opts || {}),
    }),
  stop: (id: string, generationId?: string, messageId?: string) =>
    request<StopResult>(`/sessions/${id}/stop`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ generation_id: generationId, message_id: messageId }),
    }),
  mcpTools: () => request<{ tools: { name: string; description: string }[] }>('/mcp/tools'),
  getTeam: (sessionId: string) => request<TeamSnapshot>(`/sessions/${sessionId}/team`),

  // me: LLM configs
  listLlmModels: () => request<{ models: LlmModelOption[] }>('/me/llm-models'),
  listLlmConfigs: () => request<LLMConfig[]>('/me/llm-configs'),
  createLlmConfig: (body: Partial<LLMConfig> & { api_key?: string }) =>
    request<LLMConfig>('/me/llm-configs', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateLlmConfig: (id: string, body: Partial<LLMConfig> & { api_key?: string }) =>
    request<LLMConfig>(`/me/llm-configs/${id}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteLlmConfig: (id: string) => request<{ ok: boolean }>(`/me/llm-configs/${id}`, { method: 'DELETE' }),

  // me: MCP servers
  listMcpServers: () => request<MCPServer[]>('/me/mcp-servers'),
  createMcpServer: (body: Partial<MCPServer>) =>
    request<MCPServer>('/me/mcp-servers', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateMcpServer: (id: string, body: Partial<MCPServer>) =>
    request<MCPServer>(`/me/mcp-servers/${id}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteMcpServer: (id: string) => request<{ ok: boolean }>(`/me/mcp-servers/${id}`, { method: 'DELETE' }),
  mcpEffective: () => request<{ tools: string[] }>('/me/mcp-effective'),
  mcpImport: (raw: string) =>
    request<{
      ok: boolean
      added: string[]
      updated: string[]
      skipped: string[]
      errors: string[]
      total_servers: number
      message: string
    }>('/me/mcp-servers/import', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ raw }),
    }),
  mcpExport: () => request<{ json: string }>('/me/mcp-servers/export'),

  // ---------------- A2A per-user access ----------------
  getA2AAgent: () => request<A2AAgentInfo>('/a2a/agent'),
  setA2AEnabled: (enabled: boolean) =>
    request<A2AAgentInfo>('/a2a/agent/enabled', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ enabled }),
    }),
  listA2ATokens: () => request<{ tokens: A2AToken[] }>('/a2a/tokens'),
  createA2AToken: (body: {
    name?: string
    scopes?: string[]
    expires_in_days?: number | null
    tool_policy?: string
  }) =>
    request<A2ATokenCreated>('/a2a/tokens', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }),
  revokeA2AToken: (id: string) =>
    request<{ ok: boolean }>(`/a2a/tokens/${id}/revoke`, { method: 'POST' }),

  // ---------------- workspace 文件 ----------------
  workspaceFiles: (path = '', depth = 2, limit = 500) =>
    request<WorkspaceListing>(
      `/me/workspace-files?path=${encodeURIComponent(path)}&depth=${depth}&limit=${limit}`,
    ),

  // me: skills
  listSkills: () => request<Skill[]>('/me/skills'),
  getSkill: (name: string) =>
    request<Skill & { content: string }>(`/me/skills/${encodeURIComponent(name)}`),
  listBuiltinSkills: () => request<Skill[]>('/skills'),
  listSharedSkills: () => request<Skill[]>('/skills/shared'),
  importSkillZip: (file: File, overwrite = false) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ ok: boolean; installed: string[] }>(
      `/me/skills/import?overwrite=${overwrite ? 'true' : 'false'}`,
      { method: 'POST', body: fd },
    )
  },
  createSkill: (body: { name: string; description?: string; content?: string; document?: string }) =>
    request<Skill>('/me/skills', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateSkill: (name: string, body: { name?: string; description?: string; content?: string; document?: string }) =>
    request<Skill>(`/me/skills/${encodeURIComponent(name)}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteSkill: (name: string) =>
    request<{ ok: boolean }>(`/me/skills/${encodeURIComponent(name)}`, { method: 'DELETE' }),
}


function parseFrame(raw: string): { event: string; data: StreamEvent } | null {
  let event = 'message'
  let dataLine = ''
  for (const line of raw.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLine += line.slice(5).trim()
  }
  if (!dataLine) return null
  try {
    const data = JSON.parse(dataLine) as Record<string, unknown>
    // team_event 的 data 本身就是持久化 Team 事件，其中 `type` 是
    // team_start / task_created 等业务类型。若直接用外层 SSE 名覆盖会丢业务类型，
    // 若保留业务类型又会让 App 永远匹配不到 `team_event` 分支。
    // 因此与 subagent_event 一样包一层：外层 type 表示协议，event 保存业务事件。
    if (event === 'team_event') {
      return {
        event,
        data: { type: 'team_event', event: data } as unknown as StreamEvent,
      }
    }
    // 其余事件后端把类型放在 SSE 的 `event:` 行，data 里通常没有 type 字段。
    // 这里以事件名为准补齐 type，否则前端所有分支都匹配不上（历史 bug）。
    if (event && event !== 'message' && typeof data.type !== 'string') {
      data.type = event
    }
    return { event, data: data as unknown as StreamEvent }
  } catch {
    return null
  }
}

export async function uploadImage(file: File): Promise<UploadedImage> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${BASE}/me/uploads`, {
    method: 'POST',
    headers: authHeaders(),
    body: form,
  })
  if (!res.ok) {
    let detail = `上传失败 (${res.status})`
    try {
      const body = await res.json()
      detail = body.detail || body.error || detail
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return (await res.json()) as UploadedImage
}

export async function streamMessage(
  sessionId: string,
  text: string,
  generationId: string,
  onEvent: (ev: StreamEvent) => void,
  signal?: AbortSignal,
  attachments?: Attachment[],
  reasoningEffort?: string,
): Promise<void> {
  const res = await fetch(`${BASE}/sessions/${sessionId}/messages`, {
    method: 'POST',
    headers: authHeaders({ 'content-type': 'application/json', accept: 'text/event-stream' }),
    body: JSON.stringify({
      text,
      generation_id: generationId,
      ...(attachments && attachments.length ? { attachments } : {}),
      // 会话设置已有时后端会用会话值；这里带上是为了「还没建会话就先选了档」的场景
      ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}),
    }),
    signal,
  })
  if (!res.ok || !res.body) {
    if (res.status === 401) {
      setToken('')
      window.dispatchEvent(new Event('auth:logout'))
    }
    let msg = `stream failed (${res.status})`
    try {
      const b: unknown = await res.json()
      const obj = b && typeof b === 'object' ? b as Record<string, unknown> : {}
      msg = detailText(obj.detail ?? obj.error, msg)
    } catch {
      /* ignore */
    }
    onEvent({ type: 'error', error: msg })
    return
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx = buf.indexOf('\n\n')
    while (idx !== -1) {
      const raw = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const frame = parseFrame(raw)
      if (frame) onEvent(frame.data)
      idx = buf.indexOf('\n\n')
    }
  }
  if (buf.trim()) {
    const frame = parseFrame(buf)
    if (frame) onEvent(frame.data)
  }
}
