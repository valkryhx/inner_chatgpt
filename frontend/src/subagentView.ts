/**
 * 子代理卡片的纯视图逻辑：去重、状态单调、耗时、失败原因、列表截断。
 * 放在这里是为了能用 node --test 直接验证（组件只负责渲染）。
 */

export type SubagentStatus =
  | 'running'
  | 'stopping'
  | 'completed'
  | 'errored'
  | 'stopped'
  | 'closed'
  | string

const TERMINAL = new Set(['completed', 'errored', 'stopped', 'closed'])

/** 终态：状态不会再变 */
export function isTerminal(status: SubagentStatus): boolean {
  return TERMINAL.has(status)
}

/** 状态只前进不后退：终态不会被 running / stopping 覆盖 */
export function mergeStatus(prev: SubagentStatus, next: SubagentStatus): SubagentStatus {
  if (isTerminal(prev)) return prev
  return next || prev
}

export interface StartEvent {
  agent_id: string
  name?: string
  role?: string
  task?: string
  startedAt?: number
}

/**
 * 收到 subagent_start 时并入列表：同一 agent_id 只保留一行。
 * 断线重连 / 事件重放会重复下发 start，直接 append 会出现两行同名卡片。
 */
export function mergeStart<T extends { agent_id: string }>(blocks: T[], ev: StartEvent): T[] {
  const i = blocks.findIndex((b) => b.agent_id === ev.agent_id)
  if (i < 0) {
    return [
      ...blocks,
      {
        agent_id: ev.agent_id,
        name: ev.name || ev.agent_id,
        role: ev.role || 'general',
        task: ev.task || '',
        status: 'running',
        events: [],
        result: '',
        turns: 0,
        startedAt: ev.startedAt,
        endedAt: undefined,
      } as unknown as T,
    ]
  }
  const out = blocks.slice()
  const cur = { ...out[i] } as Record<string, unknown>
  cur.name = ev.name || cur.name
  cur.startedAt = cur.startedAt || ev.startedAt
  out[i] = cur as unknown as T
  return out
}

export interface DurationBlock {
  startedAt?: number
  endedAt?: number
}

/** 毫秒耗时：运行中按 now 算，结束后固定在 endedAt；没有起点返回 null（不伪造） */
export function elapsedMs(block: DurationBlock, now: number): number | null {
  if (typeof block.startedAt !== 'number') return null
  const end = typeof block.endedAt === 'number' ? block.endedAt : now
  return Math.max(0, end - block.startedAt)
}

/** 人类可读耗时：42s / 1:23 / 1:02:05 */
export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  if (m > 0) return `${m}:${String(s).padStart(2, '0')}`
  return `${s}s`
}

export interface ErrorHit {
  source: 'tool' | 'error'
  name?: string
  text: string
}

/** 失败原因：最后一条工具错误优先，没有就退回 block.error */
export function lastToolError(block: {
  error?: string
  events?: Array<{ kind?: string; name?: string; result?: string; is_error?: boolean }>
}): ErrorHit | null {
  const events = block.events || []
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const ev = events[i]
    if (ev.kind === 'tool_result' && ev.is_error) {
      return { source: 'tool', name: ev.name, text: ev.result || '' }
    }
  }
  if (block.error && block.error.trim()) return { source: 'error', text: block.error }
  return null
}

/** 列表截断：超过 limit 时只渲染前 limit 条，并返回隐藏条数用于显式提示 */
export function capList<T>(items: T[], limit: number): { items: T[]; hidden: number } {
  if (limit <= 0 || items.length <= limit) return { items, hidden: 0 }
  return { items: items.slice(0, limit), hidden: items.length - limit }
}
