import { useEffect, useRef, useState } from 'react'
import type { LlmModelOption } from '../types'

interface Props {
  models: LlmModelOption[]
  currentId: string
  disabled?: boolean
  onChange: (id: string) => void
}

/** 自定义模型下拉：闭合态显示「label · model」，菜单里再标默认，避免把「（默认）」塞进值里。 */
export default function ModelMenu({ models, currentId, disabled, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const current = models.find((m) => m.id === currentId)
  const label = current
    ? `${current.label} · ${current.model}`
    : models.length === 0
      ? '未配置模型'
      : '选择模型'

  return (
    <div className="model-menu" ref={ref}>
      <button
        type="button"
        className="model-menu-btn"
        disabled={disabled || models.length === 0}
        onClick={() => setOpen((v) => !v)}
        title={label}
      >
        <span className="mm-label">{label}</span>
        <span className="mm-caret" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="model-menu-pop" role="listbox">
          {models.map((m) => (
            <button
              type="button"
              key={m.id}
              className={'mm-item' + (m.id === currentId ? ' active' : '')}
              onClick={() => { onChange(m.id); setOpen(false) }}
              title={`${m.label} · ${m.model}`}
            >
              <span className="mm-name">{m.label} · {m.model}</span>
              {m.is_default && <span className="mm-default">默认</span>}
              {m.id === currentId && <span className="mm-check" aria-hidden="true">✓</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
