/**
 * Team 事件 → 面板快照的合并逻辑。
 *
 * 抽成独立模块是为了能直接测（原来内联在 App.tsx 里，测试只能靠字符串匹配）。
 *
 * ## 为什么需要「自举」（2026-11-19 用户报告）
 *
 * 用户看到右侧栏一直「正在启动...」直到整轮 team 任务跑完。实测根因：
 *
 * 续看端点 `GET /api/sessions/{id}/stream?after=0` 走 `turn_job.sync_frames(0)`，
 * 该函数在「after 太老」时只发一帧 `sync` 快照，**不重放历史 team_event**；
 * 前端处理 `sync` 只替换 blocks，不重建 teamSnapshot。
 *
 * 于是前端永远拿不到 `team_start`，而原实现有这一行：
 *
 * ```ts
 * if (!prev || !prev.team) return prev    // 没有 team 对象，后续事件全丢弃
 * ```
 *
 * 所以 `task_created` 全部被丢掉 → team 恒为 null →
 * `starting = streaming && !team` 恒为真 → 侧栏卡住。
 *
 * 修复：允许从带 `team_id` 的业务事件自举出一个最小 team 对象。
 * 自举状态是 `active`，等真正的 `team_end` 再更新。
 */
import type { TeamEvent, TeamSnapshot } from './types'

function bootstrapTeam(event: TeamEvent): TeamSnapshot['team'] {
  const now = Date.now()
  const payload = event.payload || {}
  return {
    team_id: event.team_id,
    user_id: '',
    session_id: event.session_id || '',
    generation_id: event.generation_id || '',
    lead_agent_id: event.agent_id || 'root',
    name: typeof payload.name === 'string' ? payload.name : 'Agent Team',
    status: 'active',
    created_at: now,
    updated_at: now,
    closed_at: 0,
  }
}

/** 这些事件带 team_id，可以作为「team 存在」的证据来自举。 */
const BOOTSTRAP_TYPES = new Set([
  'task_created', 'task_updated', 'team_message', 'team_resumed', 'team_end',
])

export function mergeTeamEvent(
  prev: TeamSnapshot | null,
  event: TeamEvent,
): TeamSnapshot | null {
  // Team creation can fail (quota/DB) while the ordinary turn still runs.  Keep
  // the event even when no team snapshot exists, so the panel and the notice
  // bar can explain why no task board appeared.
  if (event.type === 'team_unavailable') {
    const base = prev || { team: null, tasks: [], events: [], messages: [] }
    const events = [...base.events.filter((e) => e.event_seq !== event.event_seq), event]
      .sort((a, b) => a.event_seq - b.event_seq)
    return { team: base.team, tasks: base.tasks, events, messages: base.messages }
  }
  // 新 Team 的 team_start 必须能替换旧代快照，否则同一会话下一轮生成期间
  // 会一直显示上一轮的旧任务板（generation/team id 都不同）。
  if (event.type === 'team_start' && (!prev?.team || prev.team.team_id !== event.team_id)) {
    return {
      team: {
        team_id: event.team_id, user_id: '', session_id: event.session_id || '',
        generation_id: event.generation_id || '', lead_agent_id: event.agent_id || 'root',
        name: typeof event.payload.name === 'string' ? event.payload.name : 'Agent Team',
        status: 'active', created_at: Date.now(), updated_at: Date.now(), closed_at: 0,
      },
      tasks: [], events: [event], messages: [],
    }
  }

  // 缺 team_start（续看/sync 路径不发它）时，从后续业务事件自举。
  // 不自举的后果见模块头：整个任务板会被丢弃到本轮结束。
  const bootstrapped = !prev || !prev.team
  if (bootstrapped) {
    if (!event.team_id || !BOOTSTRAP_TYPES.has(event.type)) return prev
    prev = { team: bootstrapTeam(event), tasks: [], events: [], messages: [] }
  }

  if (event.team_id && prev.team && prev.team.team_id !== event.team_id) return prev
  const events = prev.events.some((e) => e.event_seq === event.event_seq)
    ? prev.events
    : [...prev.events, event].sort((a, b) => a.event_seq - b.event_seq)
  let tasks = prev.tasks
  let messages = prev.messages
  let team = prev.team
  if (event.type === 'team_resumed') {
    // The backend snapshots the previous generation's open board.  Keep the
    // restored rows in the same snapshot so the panel updates on first paint.
    const restoredTasks = event.payload.tasks
    const restoredMessages = event.payload.messages
    if (Array.isArray(restoredTasks)) tasks = restoredTasks as TeamSnapshot['tasks']
    if (Array.isArray(restoredMessages)) messages = restoredMessages as TeamSnapshot['messages']
  } else if (event.type === 'task_created' || event.type === 'task_updated') {
    const task = event.payload.task as TeamSnapshot['tasks'][number] | undefined
    if (task?.task_id) {
      const exists = tasks.some((t) => t.task_id === task.task_id)
      tasks = exists ? tasks.map((t) => t.task_id === task.task_id ? task : t) : [...tasks, task]
    }
  } else if (event.type === 'team_message') {
    const message = event.payload.message as TeamSnapshot['messages'][number] | undefined
    if (message?.message_id && !messages.some((m) => m.message_id === message.message_id)) {
      messages = [...messages, message]
    }
  } else if (event.type === 'team_end') {
    const nextStatus = typeof event.payload.status === 'string'
      ? event.payload.status as typeof team.status
      : team.status
    team = { ...team, status: nextStatus }
  }
  return { team, tasks, events, messages }
}