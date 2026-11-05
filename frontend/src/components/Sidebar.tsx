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
          <div className="user-info" title={user.email}>
            <span className="avatar">{(user.name || user.email || '?').slice(0, 1).toUpperCase()}</span>
            <span className="user-name">{user.name || user.email}</span>
          </div>
          <div className="user-actions">
            <button className="ghost-btn small" onClick={onOpenSettings}>
              设置
            </button>
            <button className="ghost-btn small danger" onClick={onLogout}>
              登出
            </button>
          </div>
        </div>
      </div>
    </aside>
  )
}
