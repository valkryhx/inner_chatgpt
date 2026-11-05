import type {
  AuthResponse,
  LLMConfig,
  LlmModelOption,
  MCPServer,
  Session,
  SessionDetail,
  Message,
  StreamEvent,
  User,
  Skill,
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
      const body = await res.json()
      detail = body.detail || body.error || detail
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
  listSessions: () => request<{ sessions: Session[]; has_more: boolean }>('/sessions?limit=100'),
  createSession: (title?: string, modelId?: string) =>
    request<Session>('/sessions', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ title, model_id: modelId ?? '' }),
    }),
  getSession: (id: string) => request<SessionDetail>(`/sessions/${id}`),
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
  stop: (id: string, generationId?: string) =>
    request<{ ok: boolean }>(`/sessions/${id}/stop`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ generation_id: generationId }),
    }),
  mcpTools: () => request<{ tools: { name: string; description: string }[] }>('/mcp/tools'),

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
    // 后端把事件类型放在 SSE 的 `event:` 行，data 里通常没有 type 字段。
    // 这里以事件名为准补齐 type，否则前端所有分支都匹配不上（历史 bug）。
    if (event && event !== 'message' && typeof data.type !== 'string') {
      data.type = event
    }
    return { event, data: data as unknown as StreamEvent }
  } catch {
    return null
  }
}

export async function streamMessage(
  sessionId: string,
  text: string,
  generationId: string,
  onEvent: (ev: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE}/sessions/${sessionId}/messages`, {
    method: 'POST',
    headers: authHeaders({ 'content-type': 'application/json', accept: 'text/event-stream' }),
    body: JSON.stringify({ text, generation_id: generationId }),
    signal,
  })
  if (!res.ok || !res.body) {
    if (res.status === 401) {
      setToken('')
      window.dispatchEvent(new Event('auth:logout'))
    }
    let msg = `stream failed (${res.status})`
    try {
      const b = await res.json()
      msg = b.detail || b.error || msg
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
