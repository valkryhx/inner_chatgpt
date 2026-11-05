import { useRef, useState } from 'react'

interface Props {
  streaming: boolean
  onSend: (text: string) => void
  onStop: () => void
  onCompact?: (instructions: string) => void
}

export default function Composer({ streaming, onSend, onStop, onCompact }: Props) {
  const [text, setText] = useState('')
  const taRef = useRef<HTMLTextAreaElement>(null)

  const clear = () => {
    setText('')
    if (taRef.current) taRef.current.style.height = 'auto'
  }

  const submit = () => {
    if (streaming) return
    const t = text.trim()
    if (!t) return
    // 输入框指令：/compact [自定义要求] —— 手动压缩上下文，不作为普通消息发送
    const m = t.match(/^\/compact\s*([\s\S]*)$/i)
    if (m) {
      onCompact?.(m[1].trim())
      clear()
      return
    }
    onSend(t)
    clear()
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  const autoGrow = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value)
    e.target.style.height = 'auto'
    e.target.style.height = Math.min(e.target.scrollHeight, 200) + 'px'
  }

  return (
    <div className="composer-wrap">
      <div className="composer">
        <textarea
          ref={taRef}
          value={text}
          onChange={autoGrow}
          onKeyDown={onKeyDown}
          placeholder="输入消息，Enter 发送，Shift+Enter 换行；/compact 手动压缩上下文"
          rows={1}
          disabled={streaming}
        />
        {streaming ? (
          <button className="send-btn stop" onClick={onStop}>
            停止
          </button>
        ) : (
          <button className="send-btn" onClick={submit} disabled={!text.trim()}>
            发送
          </button>
        )}
      </div>
      <div className="composer-hint">
        自研 Agent 内核 · 工具由 FastMCP 提供 · 会话持久化于 SQLite
      </div>
    </div>
  )
}
