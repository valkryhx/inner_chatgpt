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
  onOpenAllSessions: () => void
  /** 打开历史对话正文搜索面板。 */
  onOpenSearch: () => void
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
  onOpenAllSessions,
  onOpenSearch,
}: Props) {
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [hidden, setHidden] = useState(false)
  const displayName = user.name || user.username || user.email || '未命名用户'
  const showEmail = !!user.email && user.email !== displayName
  // health.servers 来自 /mcp-effective，与对话轮次共用同一份 runtime。
  const servers = health?.servers || []
  const badServers = servers.filter((s) => !s.ok)

  const pinned = sessions.filter((s) => s.pinned)
  const rest = sessions.filter((s) => !s.pinned)

  const startEdit = (s: Session) => {
    setEditingId(s.id)
    setDraft(s.title)
  }
  const commitEdit = () => {
    if (editingId && draft.trim()) onRename(editingId, draft.trim())
    setEditingId(null)
  }

  const row = (s: Session) => (
    <div
      key={s.id}
      className={'session-item' + (s.id === activeId ? ' active' : '')}
      onClick={() => onSelect(s.id)}
      onDoubleClick={() => startEdit(s)}
      title={s.title || '双击重命名'}
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
            background: 'var(--surface)',
            border: '1px solid var(--clay)',
            borderRadius: 6,
            color: 'var(--ink)',
            padding: '3px 6px',
            fontSize: 13,
          }}
        />
      ) : (
        <>
          <div className="session-title">{s.title || '新会话'}</div>
          <button
            className="session-del"
            title="删除（双击可先重命名）"
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
  )

  if (hidden) {
    return (
      <button className="sidebar-reopen" title="显示侧边栏" onClick={() => setHidden(false)}>
        ☰
      </button>
    )
  }

  return (
    <aside className="sidebar">
      <div className="sidebar-top">
        <button className="icon-btn" title="隐藏侧边栏" onClick={() => setHidden(true)}>☰</button>
        <span className="brand">Hlaudz</span>
      </div>

      <nav className="side-nav">
        <button className="side-nav-item" onClick={onCreate}>
          <span aria-hidden="true">＋</span> 新建 <span className="kbd">Ctrl Shift O</span>
        </button>
        <button className="side-nav-item" onClick={onOpenSearch}>
          <span aria-hidden="true">⌕</span> 搜索 <span className="kbd">Ctrl Shift K</span>
        </button>
        <button className="side-nav-item" disabled title="项目（未实现）">
          <span aria-hidden="true">▤</span> 项目
        </button>
        <button className="side-nav-item" disabled title="Artifacts（未实现）">
          <span aria-hidden="true">◫</span> Artifacts
        </button>
        <button className="side-nav-item" disabled title="Code（未实现）">
          <span aria-hidden="true">‹›</span> Code <span className="tag">未开通</span>
        </button>
        <button className="side-nav-item" onClick={onOpenSettings}>
          <span aria-hidden="true">⚙</span> 自定义
        </button>
      </nav>

      {pinned.length > 0 && (
        <>
          <div className="side-section"><span>已置顶</span></div>
          <div className="session-list">{pinned.map(row)}</div>
        </>
      )}

      <div className="side-section">
        <span>对话和任务</span>
        <button className="icon-btn" disabled title="分组和排序（未实现）" aria-label="分组和排序">⇅</button>
      </div>
      <div className="session-list">
        {rest.length === 0 && pinned.length === 0 && (
          <div style={{ color: 'var(--muted)', fontSize: 12, padding: '8px' }}>还没有会话</div>
        )}
        {rest.map(row)}
      </div>
      <button className="side-nav-item all-sessions" onClick={onOpenAllSessions}>
        查看全部会话
      </button>

      <button
        type="button"
        className="mcp-health"
        onClick={onOpenSettings}
        title={
          badServers.length
            ? `连接失败：${badServers.map((s) => s.name).join('、')}（点击查看详情）`
            : 'MCP 与工具状态（点击打开设置）'
        }
      >
        <span className={`mcp-dot ${badServers.length ? 'warn' : 'ok'}`} />
        <span className="mcp-health-text">
          {servers.length === 0
            ? 'MCP 未检测'
            : badServers.length
            ? `${badServers.length} 个 MCP 断开`
            : `MCP 正常 · ${health?.mcp_tools ?? 0} 工具`}
        </span>
      </button>

      <div className="sidebar-footer">
        <div className="user-bar">
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
          </button>
          <button type="button" className="icon-btn" onClick={onLogout} title="登出" aria-label="登出">⏻</button>
        </div>
      </div>
    </aside>
  )
}
