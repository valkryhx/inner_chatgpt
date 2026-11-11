import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { Session } from '../types'

interface Props {
  open: boolean
  activeId: string | null
  onClose: () => void
  onSelect: (id: string) => void
  onDelete: (id: string) => void
  /** 打开时先展示 App 已加载的那份，避免白屏 */
  initial?: Session[]
}

function fmt(ts: number): string {
  return new Date(ts).toLocaleString('zh-CN', { hour12: false })
}

/** 对标 claude.ai 侧栏的「查看全部」：全量会话浏览 + 点击即恢复（resume）。 */
export default function SessionBrowser({ open, activeId, onClose, onSelect, onDelete, initial }: Props) {
  const [items, setItems] = useState<Session[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [q, setQ] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
    setQ('')
    setError('')
    setItems(initial || [])
    setLoading(true)
    api.listSessions(100, 0)
      .then((r) => { setItems(r.sessions); setHasMore(r.has_more) })
      .catch((e: any) => setError(e?.message || String(e)))
      .finally(() => setLoading(false))
  }, [open])

  const more = async () => {
    setLoading(true)
    try {
      const r = await api.listSessions(100, items.length)
      setItems((prev) => [...prev, ...r.sessions])
      setHasMore(r.has_more)
    } catch (e: any) {
      setError(e?.message || String(e))
    } finally {
      setLoading(false)
    }
  }

  const shown = useMemo(() => {
    const k = q.trim().toLowerCase()
    if (!k) return items
    return items.filter((s) => (s.title || '').toLowerCase().includes(k))
  }, [items, q])

  if (!open) return null

  return (
    <div className="modal-mask" onClick={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="modal session-browser" role="dialog" aria-label="全部会话">
        <div className="modal-head">
          <h2>全部会话</h2>
          <button className="ghost-btn" onClick={onClose}>关闭</button>
        </div>
        <div className="sb-search">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="按标题筛选已加载的会话…（搜正文请用 ⌕ 搜索）"
            autoFocus
          />
        </div>
        <div className="sb-list">
          {error && <div className="auth-error">{error}</div>}
          {shown.length === 0 && !loading && <div className="hint">没有匹配的会话</div>}
          {shown.map((s) => (
            <div
              key={s.id}
              className={'sb-row' + (s.id === activeId ? ' active' : '')}
              onClick={() => { onSelect(s.id); onClose() }}
              title="点击恢复这个会话"
            >
              <div className="sb-main">
                <div className="sb-title">{s.title || '新会话'}</div>
                <div className="sb-meta">
                  {s.pinned ? '已置顶 · ' : ''}{fmt(s.updated_at)}
                </div>
              </div>
              <button
                className="icon-btn"
                title="删除"
                onClick={(e) => {
                  e.stopPropagation()
                  onDelete(s.id)
                  setItems((prev) => prev.filter((x) => x.id !== s.id))
                }}
              >
                ×
              </button>
            </div>
          ))}
          {hasMore && (
            <button className="ghost-btn sb-more" onClick={more} disabled={loading}>
              {loading ? '加载中…' : '加载更多'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
