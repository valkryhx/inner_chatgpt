import { useEffect, useRef, useState } from 'react'

import { effortLabel, menuOptions } from '../reasoningLevels'

interface Props {
  value: string
  disabled?: boolean
  onChange: (value: string) => void
}

/**
 * 思考等级下拉：放在模型选择器后面。
 * 选「不指定」= 不覆盖，沿用模型配置里配的 reasoning_effort。
 */
export default function ReasoningMenu({ value, disabled, onChange }: Props) {
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

  const options = menuOptions()
  const label = effortLabel(value)

  return (
    <div className="model-menu" ref={ref}>
      <button
        type="button"
        className="model-menu-btn"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        title={`思考等级：${label}`}
      >
        <span className="mm-label">思考 {label}</span>
        <span className="mm-caret" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="model-menu-pop" role="listbox">
          {options.map((opt) => (
            <button
              type="button"
              key={opt.value || '__auto__'}
              className={'mm-item' + (opt.value === value ? ' active' : '')}
              onClick={() => { onChange(opt.value); setOpen(false) }}
            >
              <span className="mm-name">{opt.label}</span>
              {opt.value === value && <span className="mm-check" aria-hidden="true">✓</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
