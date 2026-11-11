import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import type { MessageSearchHit } from '../types'

interface Props {
  open: boolean
  onClose: () => void
  /** 点结果：切到该会话，并把命中的那条消息滚到视野中央。 */
  onOpenHit: (hit: MessageSearchHit) => void
}

function fmt(ts: number): string {
  // created_at 可能缺失/非数字（旧数据或被改坏的响应），别让 toLocaleString 抛异常。
  const n = typeof ts === 'number' && Number.isFinite(ts) ? ts : 0
  if (!n) return ''
  try {
    return new Date(n).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return ''
  }
}

/**
 * 把服务端返回的一条命中规整成组件可安全渲染的形状。
 *
 * 这是白屏的第一道防线（第二道是 ErrorBoundary）：脏数据在这里被消化掉，
 * 而不是让它在渲染期抛异常、再去指望边界兜。宁可少显示字段，
 * 也不能让一条脏数据击穿整棵组件树。
 */
function normalizeHit(raw: unknown): MessageSearchHit | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const messageId = typeof r.message_id === 'string' ? r.message_id : ''
  const sessionId = typeof r.session_id === 'string' ? r.session_id : ''
  // 没有 id 的结果点不动、也定位不到，直接丢弃。
  if (!messageId || !sessionId) return null

  const snippet = typeof r.snippet === 'string' ? r.snippet : ''
  const offset = typeof r.match_offset === 'number' && Number.isFinite(r.match_offset)
    ? Math.trunc(r.match_offset)
    : -1
  const length = typeof r.match_length === 'number' && Number.isFinite(r.match_length)
    ? Math.max(0, Math.trunc(r.match_length))
    : 0

  return {
    message_id: messageId,
    session_id: sessionId,
    session_title: typeof r.session_title === 'string' ? r.session_title : '',
    role: r.role === 'assistant' ? 'assistant' : r.role === 'user' ? 'user' : 'system',
    snippet,
    // 越界的 offset 一律降级成「不高亮」，避免 slice 出错位文本。
    match_offset: offset >= 0 && offset + length <= snippet.length ? offset : -1,
    match_length: length,
    created_at: typeof r.created_at === 'number' && Number.isFinite(r.created_at)
      ? r.created_at
      : 0,
    session_updated_at: typeof r.session_updated_at === 'number'
      && Number.isFinite(r.session_updated_at) ? r.session_updated_at : 0,
  }
}

/** 把整个响应规整成 { hits, hasMore }，任何异常形状都不抛。 */
function normalizeResult(raw: unknown): { hits: MessageSearchHit[]; hasMore: boolean } {
  if (!raw || typeof raw !== 'object') return { hits: [], hasMore: false }
  const r = raw as Record<string, unknown>
  const list = Array.isArray(r.hits) ? r.hits : []
  const hits: MessageSearchHit[] = []
  for (const item of list) {
    const h = normalizeHit(item)
    if (h) hits.push(h)
  }
  return { hits, hasMore: r.has_more === true }
}

/** 把片段按命中位置切成三段，中间那段高亮。 */
function Snippet({ hit }: { hit: MessageSearchHit }) {
  const { snippet, match_offset: off, match_length: len } = hit
  // offset 为 -1 或越界时不高亮，但正文照常显示——宁可少个高亮，也不要吞掉内容。
  const ok = off >= 0 && len > 0 && off + len <= snippet.length
  if (!ok) return <>{snippet}</>
  return (
    <>
      {snippet.slice(0, off)}
      <mark className="ss-mark">{snippet.slice(off, off + len)}</mark>
      {snippet.slice(off + len)}
    </>
  )
}

/** 对标 claude.ai 的「搜索」：按正文关键词检索历史对话，点结果跳到那条消息。 */
export default function SessionSearch({ open, onClose, onOpenHit }: Props) {
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<MessageSearchHit[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [elapsed, setElapsed] = useState(0)
  const [cursor, setCursor] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  // 每次搜索自增，用来丢弃迟到的旧响应（快速连打时后发先至会把结果覆盖错）。
  const seqRef = useRef(0)

  const reset = useCallback(() => {
    setQ('')
    setHits([])
    setHasMore(false)
    setError('')
    setElapsed(0)
    setCursor(0)
  }, [])

  useEffect(() => {
    if (!open) return
    reset()
    // 弹窗刚挂载时 autoFocus 在部分浏览器上抢不到焦点，补一帧。
    const id = window.setTimeout(() => inputRef.current?.focus(), 0)
    return () => window.clearTimeout(id)
  }, [open, reset])

  const run = useCallback(async (text: string) => {
    const keyword = text.trim()
    if (!keyword) {
      seqRef.current += 1
      setHits([])
      setHasMore(false)
      setCursor(0)
      setElapsed(0)
      setError('')
      return
    }
    const seq = ++seqRef.current
    setLoading(true)
    setError('')
    try {
      const r = await api.searchMessages(keyword, 30)
      if (seq !== seqRef.current) return
      // 不信任线上响应的形状：规整失败也不能抛，否则整个应用白屏。
      const { hits: safeHits, hasMore: safeMore } = normalizeResult(r)
      setHits(safeHits)
      setHasMore(safeMore)
      setElapsed(
        typeof (r as { elapsed_ms?: unknown })?.elapsed_ms === 'number'
          ? (r as { elapsed_ms: number }).elapsed_ms
          : 0,
      )
      setCursor(0)
    } catch (e: any) {
      if (seq !== seqRef.current) return
      setError(e?.message || String(e))
      setHits([])
      setHasMore(false)
    } finally {
      if (seq === seqRef.current) setLoading(false)
    }
  }, [])

  // 输入防抖：每敲一个字都打后端会把 314 条命中翻来覆去地查。
  useEffect(() => {
    if (!open) return
    const id = window.setTimeout(() => { void run(q) }, 220)
    return () => window.clearTimeout(id)
  }, [q, open, run])

  // ↑↓ 选结果，Enter 打开，Esc 关闭——键盘全程不用离开输入框。
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      onClose()
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setCursor((c) => (hits.length ? Math.min(c + 1, hits.length - 1) : 0))
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      setCursor((c) => Math.max(c - 1, 0))
      return
    }
    if (e.key === 'Enter') {
      const hit = hits[cursor]
      if (hit) {
        e.preventDefault()
        onOpenHit(hit)
        onClose()
      }
    }
  }

  const summary = useMemo(() => {
    if (!q.trim()) return ''
    if (loading) return '搜索中…'
    if (error) return ''
    if (hits.length === 0) return '没有匹配的消息'
    const more = hasMore ? '（还有更多）' : ''
    return `${hits.length} 条结果${more} · ${elapsed} ms`
  }, [q, loading, error, hits.length, hasMore, elapsed])

  if (!open) return null

  return (
    <div
      className="modal-mask ss-mask"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="modal ss-panel" role="dialog" aria-label="搜索历史对话">
        <div className="ss-input-row">
          <span className="ss-icon" aria-hidden="true">⌕</span>
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="搜索历史对话的正文…"
            autoFocus
            aria-label="搜索关键词"
          />
          <button className="ghost-btn small" onClick={onClose}>关闭</button>
        </div>

        {summary && <div className="ss-summary">{summary}</div>}
        {error && <div className="auth-error ss-error">{error}</div>}

        <div className="ss-list">
          {!q.trim() && (
            <div className="hint ss-empty">
              输入关键词搜索所有历史对话的正文（含你和助手的发言）。
              用 ↑ ↓ 选择，Enter 打开。
            </div>
          )}
          {hits.map((hit, i) => (
            <button
              key={hit.message_id}
              type="button"
              className={'ss-row' + (i === cursor ? ' active' : '')}
              onMouseEnter={() => setCursor(i)}
              onClick={() => { onOpenHit(hit); onClose() }}
              title="打开这条对话并定位到该消息"
            >
              <div className="ss-row-head">
                <span className={'ss-role ' + hit.role}>
                  {hit.role === 'user' ? '你' : '助手'}
                </span>
                <span className="ss-title">{hit.session_title || '新会话'}</span>
                <span className="ss-time">{fmt(hit.created_at)}</span>
              </div>
              <div className="ss-snippet"><Snippet hit={hit} /></div>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}