import { useState } from 'react'
import type { Session, User } from '../types'

interface Props {
  sessions: Session[]
  activeId: string | null
  health: { model: string; mcp_tools: number } | null
  user: User
  activeModel?: string | null
  onSelect: (id: string) => void
  onCreate: () => void
  onDelete: (id: string) => void
  onRename: (id: string, title: string) => void
  onOpenSettings: () => void
  onLogout: () => void
}

function timeAgo(ts: number): string {
  const diff = Date.now() - ts
  const m = Math.floor(diff / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  return `${Math.floor(h / 24)} 天前`
}

export default function Sidebar({
  sessions,
  activeId,
  health,
  user,
  activeModel,
  onSelect,
  onCreate,
  onDelete,
  onRename,
  onOpenSettings,
  onLogout,
}: Props) {
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  // 用全名做主标识（不再用单字母缩写）；有 name 时才把邮箱降为副标题。
  const displayName = user.name || user.username || user.email || '未命名用户'
  const showEmail = !!user.email && user.email !== displayName

  const startEdit = (s: Session) => {
    setEditingId(s.id)
    setDraft(s.title)
  }
  const commitEdit = () => {
    if (editingId && draft.trim()) onRename(editingId, draft.trim())
    setEditingId(null)
  }

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <button className="new-btn" onClick={onCreate}>
          + 新建会话
        </button>
      </div>
      <div className="session-list">
        {sessions.length === 0 && (
          <div style={{ color: 'var(--muted)', fontSize: 12, padding: '12px 8px' }}>还没有会话</div>
        )}
        {sessions.map((s) => (
          <div
            key={s.id}
            className={'session-item' + (s.id === activeId ? ' active' : '')}
            onClick={() => onSelect(s.id)}
            onDoubleClick={() => startEdit(s)}
            title="双击重命名"
          >
            {editingId === s.id ? (
              <input
                autoFocus
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={commitEdit}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commitEdit()
                  if (e.key === 'Escape') setEditingId(null)
                }}
                onClick={(e) => e.stopPropagation()}
                style={{
                  flex: 1,
                  background: 'var(--panel)',
                  border: '1px solid var(--accent)',
                  borderRadius: 6,
                  color: 'var(--text)',
                  padding: '4px 6px',
                  fontSize: 13,
                }}
              />
            ) : (
              <>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="session-title">{s.title}</div>
                  <div className="session-meta">
                    {timeAgo(s.updated_at)}
                    {s.compaction?.compacted ? ' · 已压缩' : ''}
                  </div>
                </div>
                <button
                  className="session-del"
                  title="删除"
                  onClick={(e) => {
                    e.stopPropagation()
                    onDelete(s.id)
                  }}
                >
                  ×
                </button>
              </>
            )}
          </div>
        ))}
      </div>
      <div className="sidebar-footer">
        <div className="foot-status">
          <span className="dot" />
          {activeModel
            ? `${activeModel} · ${health?.mcp_tools ?? 0} 个工具`
            : health
              ? `${health.model || '未配置模型'} · ${health.mcp_tools} 个工具`
              : '连接中…'}
        </div>
        <div className="user-bar">
          {/* 整行可点：直接进设置。不再显示单字母缩写，用全名 + 邮箱做身份标识。 */}
          <button
            type="button"
            className="user-chip"
            onClick={onOpenSettings}
            title={user.email ? `${displayName} · ${user.email} · 打开设置` : '打开设置'}
          >
            <span className="user-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
                   strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="8.2" r="3.4" />
                <path d="M4.8 20c.9-3.6 3.8-5.6 7.2-5.6s6.3 2 7.2 5.6" />
              </svg>
            </span>
            <span className="user-meta">
              <span className="user-display">{displayName}</span>
              {showEmail && <span className="user-email">{user.email}</span>}
            </span>
            <span className="user-gear" aria-hidden="true">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
                   strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="2.9" />
                <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.55-1.11 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34H9a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87V9a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51 1z" />
              </svg>
            </span>
          </button>
          <button type="button" className="user-logout" onClick={onLogout} title="登出">
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
                 strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M14.5 4.5H18a1.5 1.5 0 0 1 1.5 1.5v12a1.5 1.5 0 0 1-1.5 1.5h-3.5" />
              <path d="M10 16l-4-4 4-4" />
              <path d="M6 12h9.5" />
            </svg>
            <span>登出</span>
          </button>
        </div>
      </div>
    </aside>
  )
}
