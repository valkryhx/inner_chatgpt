import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { WorkspaceEntry } from '../types'
import { downloadWorkspaceFile, downloadWorkspaceZip, fileKind } from '../files'

interface Props {
  open: boolean
  onClose: () => void
}

function fmtSize(n: number): string {
  if (!n) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function fmtTime(sec: number): string {
  if (!sec) return ''
  const d = new Date(sec * 1000)
  const pad = (x: number) => String(x).padStart(2, '0')
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/**
 * 工作区文件面板：浏览 / 下载 / 多选打包。
 * 图片与任意格式都走同一个鉴权下载端点；这里只负责列举与触发下载。
 */
export default function WorkspacePanel({ open, onClose }: Props) {
  const [dir, setDir] = useState('')
  const [entries, setEntries] = useState<WorkspaceEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState('')

  const load = useCallback(async (target: string) => {
    setLoading(true)
    setError('')
    try {
      const r = await api.workspaceFiles(target, 1, 500)
      setEntries(r.entries)
      setDir(r.root)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    setSelected(new Set())
    load('')
  }, [open, load])

  if (!open) return null

  const files = entries.filter((e) => e.type === 'file')
  const dirs = entries.filter((e) => e.type === 'dir')
  const segments = dir ? dir.split('/') : []

  const enter = (path: string) => {
    setSelected(new Set())
    load(path)
  }

  const toggle = (path: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  const toggleAll = () => {
    setSelected((prev) => (prev.size >= files.length ? new Set() : new Set(files.map((f) => f.path))))
  }

  const downloadOne = async (path: string, name: string) => {
    setError('')
    setBusy(path)
    try {
      await downloadWorkspaceFile(path, name)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }

  const zipSelected = async () => {
    if (!selected.size) return
    setError('')
    setBusy('__zip__')
    try {
      await downloadWorkspaceZip([...selected])
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="ws-mask" role="presentation" onClick={onClose}>
      <aside
        className="ws-panel"
        role="dialog"
        aria-label="工作区文件"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="ws-head">
          <div className="ws-title">
            <span className="ws-kicker">Workspace</span>
            <h2>工作区文件</h2>
          </div>
          <div className="ws-head-actions">
            <button className="ghost-btn" onClick={() => load(dir)} disabled={loading}>
              刷新
            </button>
            <button className="ghost-btn" onClick={onClose} aria-label="关闭">
              ×
            </button>
          </div>
        </header>

        <div className="ws-crumbs">
          <button className="ws-crumb" onClick={() => enter('')} disabled={!dir}>
            workspace
          </button>
          {segments.map((seg, i) => {
            const path = segments.slice(0, i + 1).join('/')
            return (
              <span key={path} className="ws-crumb-wrap">
                <span className="ws-crumb-sep">/</span>
                <button className="ws-crumb" onClick={() => enter(path)}>{seg}</button>
              </span>
            )
          })}
        </div>

        <div className="ws-body">
          {error && <div className="auth-error ws-error">{error}</div>}
          {loading && <div className="ws-empty">加载中…</div>}
          {!loading && entries.length === 0 && (
            <div className="ws-empty">这里还没有文件。让 Agent 写文件或运行代码后会自动出现在这里。</div>
          )}

          {!loading && dirs.map((d) => (
            <button key={d.path} className="ws-row ws-dir" onClick={() => enter(d.path)}>
              <span className="ws-ico ws-ico-dir" aria-hidden="true">DIR</span>
              <span className="ws-name">{d.name}</span>
              <span className="ws-meta">{d.hidden ? '子代理产物' : ''}</span>
            </button>
          ))}

          {!loading && files.map((f) => {
            const kind = fileKind(f.name)
            return (
              <div key={f.path} className={'ws-row ws-file' + (selected.has(f.path) ? ' is-selected' : '')}>
                <input
                  type="checkbox"
                  className="ws-check"
                  checked={selected.has(f.path)}
                  onChange={() => toggle(f.path)}
                  aria-label={`选择 ${f.name}`}
                />
                <button className="ws-file-main" onClick={() => downloadOne(f.path, f.name)} title={`下载 ${f.name}`}>
                  <span className={'ws-ico ws-ico-' + kind} aria-hidden="true">{kind.toUpperCase()}</span>
                  <span className="ws-name">{f.name}</span>
                  <span className="ws-meta">{fmtSize(f.size)} · {fmtTime(f.mtime)}</span>
                  <span className="ws-dl" aria-hidden="true">{busy === f.path ? '…' : '↓'}</span>
                </button>
              </div>
            )
          })}
        </div>

        <footer className="ws-foot">
          <button className="ghost-btn" onClick={toggleAll} disabled={!files.length}>
            {selected.size >= files.length && files.length > 0 ? '取消全选' : '全选文件'}
          </button>
          <span className="ws-count">已选 {selected.size} 个</span>
          <button className="new-btn small" onClick={zipSelected} disabled={!selected.size || busy === '__zip__'}>
            {busy === '__zip__' ? '打包中…' : '打包下载'}
          </button>
        </footer>
      </aside>
    </div>
  )
}
