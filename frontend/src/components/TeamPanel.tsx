import { useMemo, useState } from 'react'
import type { TeamEvent, TeamSnapshot, TeamTask, TeamTaskStatus } from '../types'

interface Props {
  snapshot: TeamSnapshot | null
  open: boolean
  onToggle: () => void
}

const TASK_LABEL: Record<TeamTaskStatus, string> = {
  pending: '待处理',
  in_progress: '进行中',
  completed: '已完成',
  cancelled: '已取消',
}

const TASK_ORDER: TeamTaskStatus[] = ['in_progress', 'pending', 'completed', 'cancelled']

const EVENT_LABEL: Record<string, string> = {
  team_start: '团队已创建',
  team_end: '团队已结束',
  task_created: '创建任务',
  task_updated: '任务更新',
  team_message: '团队消息',
  team_unavailable: '团队不可用',
}

function shortId(id: string): string {
  if (!id) return '—'
  return id.length <= 16 ? id : id.slice(0, 7) + '…' + id.slice(-4)
}

function ownerLabel(owner: string): string {
  if (!owner) return '未认领'
  return owner === 'root' || owner === 'lead' ? 'Lead' : owner.replace(/^sub:/, '')
}

function eventDetail(ev: TeamEvent): string {
  const payload = ev.payload || {}
  if (ev.type === 'task_updated' || ev.type === 'task_created') {
    const task = payload.task as Record<string, unknown> | undefined
    return typeof task?.subject === 'string' ? task.subject : ''
  }
  if (ev.type === 'team_message') {
    const message = payload.message as Record<string, unknown> | undefined
    return typeof message?.content === 'string' ? message.content : ''
  }
  if (ev.type === 'team_end') {
    return typeof payload.status === 'string' ? payload.status : ''
  }
  return ''
}

function TaskCard({ task }: { task: TeamTask }) {
  const blocked = (task.blockedBy || []).filter(Boolean)
  return (
    <article className={'team-task task-' + task.status}>
      <div className="team-task-top">
        <span className={'team-task-dot ' + task.status} />
        <span className="team-task-subject">{task.subject}</span>
        <span className="team-task-version">v{task.version}</span>
      </div>
      {task.description && <p className="team-task-desc">{task.description}</p>}
      <div className="team-task-meta">
        <span>{ownerLabel(task.owner_agent_id)}</span>
        <span>{TASK_LABEL[task.status]}</span>
        {blocked.length > 0 && <span title={blocked.join(', ')}>依赖 {blocked.length}</span>}
      </div>
      {blocked.length > 0 && (
        <div className="team-task-blocked">等待 {blocked.map(shortId).join('、')}</div>
      )}
    </article>
  )
}

export default function TeamPanel({ snapshot, open, onToggle }: Props) {
  const [tab, setTab] = useState<'tasks' | 'messages' | 'events'>('tasks')
  const tasks = snapshot?.tasks || []
  const messages = snapshot?.messages || []
  const events = snapshot?.events || []
  const team = snapshot?.team
  const counts = useMemo(() => {
    const by = { pending: 0, in_progress: 0, completed: 0, cancelled: 0 }
    for (const task of tasks) by[task.status] += 1
    return by
  }, [tasks])

  if (!snapshot || !team) return null

  const status = team.status || 'active'
  const active = status === 'active'
  return (
    <aside className={'team-panel' + (open ? ' is-open' : '')} aria-label="Agent Team 任务板">
      <button type="button" className="team-panel-rail" onClick={onToggle} title={open ? '收起任务板' : '展开任务板'}>
        <span className="team-rail-mark" />
        <span className="team-rail-label">Team</span>
        <span className="team-rail-count">{counts.in_progress + counts.pending}</span>
        <span className="team-rail-caret">{open ? '›' : '‹'}</span>
      </button>
      {open && (
        <div className="team-panel-body">
          <header className="team-head">
            <div>
              <div className="team-kicker">Agent Team</div>
              <h2>{team.name || 'Agent Team'}</h2>
            </div>
            <span className={'team-status ' + status}>
              <span className={'status-dot ' + (active ? 'running' : status === 'failed' ? 'failed' : 'done')} />
              {active ? '运行中' : status === 'completed' ? '已完成' : status === 'stopped' ? '已停止' : status === 'stale' ? '已过期' : '失败'}
            </span>
          </header>

          <div className="team-stats">
            <span><b>{tasks.length}</b> 任务</span>
            <span><b>{counts.in_progress}</b> 进行中</span>
            <span><b>{messages.length}</b> 消息</span>
          </div>

          <div className="team-tabs" role="tablist">
            <button type="button" className={tab === 'tasks' ? 'active' : ''} onClick={() => setTab('tasks')}>
              任务 <span>{tasks.length}</span>
            </button>
            <button type="button" className={tab === 'messages' ? 'active' : ''} onClick={() => setTab('messages')}>
              消息 <span>{messages.length}</span>
            </button>
            <button type="button" className={tab === 'events' ? 'active' : ''} onClick={() => setTab('events')}>
              动态
            </button>
          </div>

          <div className="team-content">
            {tab === 'tasks' && (
              tasks.length === 0 ? (
                <div className="team-empty">还没有任务。Lead 可通过 task_create 拆解工作。</div>
              ) : (
                <div className="team-task-list">
                  {TASK_ORDER.map((statusKey) => {
                    const group = tasks.filter((task) => task.status === statusKey)
                    if (group.length === 0) return null
                    return (
                      <section key={statusKey} className="team-task-group">
                        <div className="team-group-head">
                          <span>{TASK_LABEL[statusKey]}</span>
                          <span>{group.length}</span>
                        </div>
                        {group.map((task) => <TaskCard key={task.task_id} task={task} />)}
                      </section>
                    )
                  })}
                </div>
              )
            )}
            {tab === 'messages' && (
              messages.length === 0 ? (
                <div className="team-empty">暂无团队消息。</div>
              ) : (
                <div className="team-message-list">
                  {messages.map((message) => (
                    <article className="team-message" key={message.message_id}>
                      <div className="team-message-route">
                        <span>{ownerLabel(message.sender_agent_id)}</span>
                        <span>→</span>
                        <span>{ownerLabel(message.recipient_agent_id)}</span>
                        <span className="team-message-kind">{message.kind}</span>
                      </div>
                      <p>{message.content}</p>
                    </article>
                  ))}
                </div>
              )
            )}
            {tab === 'events' && (
              events.length === 0 ? (
                <div className="team-empty">暂无团队动态。</div>
              ) : (
                <ol className="team-event-list">
                  {events.slice().reverse().map((event) => (
                    <li key={event.team_id + ':' + event.event_seq}>
                      <span className="team-event-seq">#{event.event_seq}</span>
                      <span className="team-event-name">{EVENT_LABEL[event.type] || event.type}</span>
                      {eventDetail(event) && <span className="team-event-detail">{eventDetail(event)}</span>}
                    </li>
                  ))}
                </ol>
              )
            )}
          </div>
        </div>
      )}
    </aside>
  )
}
