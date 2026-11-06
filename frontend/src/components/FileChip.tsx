import { useState } from 'react'
import type { ReactNode } from 'react'
import { basename, downloadWorkspaceFile, fileKind } from '../files'

const BADGE: Record<string, string> = {
  doc: 'DOC',
  sheet: 'XLS',
  slide: 'PPT',
  pdf: 'PDF',
  img: 'IMG',
  code: '</>',
  text: 'TXT',
  data: 'BIN',
  zip: 'ZIP',
  file: 'FILE',
}

interface Props {
  path: string
  name?: string
  label?: ReactNode
}

/**
 * 对话里的 workspace 文件胶囊：点击直接用 Bearer 鉴权下载。
 * Markdown 链接 `[报告](reports/q3.docx)` 会渲染成它，而不是跳到 SPA 404。
 */
export default function FileChip({ path, name, label }: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const shown = name || basename(path) || path
  const kind = fileKind(shown)

  const onDownload = async (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (busy) return
    setBusy(true)
    setError('')
    try {
      await downloadWorkspaceFile(path, shown)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <span className={'file-chip kind-' + kind}>
      <button
        type="button"
        className="file-chip-btn"
        onClick={onDownload}
        disabled={busy}
        title={error || `下载 ${shown}`}
        aria-label={`下载 ${shown}`}
      >
        <span className="file-ico" aria-hidden="true">{BADGE[kind] || 'FILE'}</span>
        <span className="file-name">{label ?? shown}</span>
        <span className="file-action" aria-hidden="true">{busy ? '…' : '↓'}</span>
      </button>
      {error && <span className="file-chip-error">{error}</span>}
    </span>
  )
}
