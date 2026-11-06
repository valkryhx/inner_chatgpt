import { useRef, useState } from 'react'
import type { ContextUsage } from '../types'

interface Props {
  streaming: boolean
  onSend: (text: string) => void
  onStop: () => void
  onCompact?: (instructions: string) => void
  contextUsage?: ContextUsage | null
  modelName?: string
  reasoningEffort?: string
}

function fmtK(n: number): string {
  return `${(Math.max(0, n) / 1000).toFixed(1)}k`
}

export default function Composer({ streaming, onSend, onStop, onCompact, contextUsage, modelName, reasoningEffort }: Props) {
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
      <div
        className="composer-hint"
        title={contextUsage
          ? `自动压缩阈值 ${fmtK(contextUsage.auto_compact_tokens)} · 上下文窗口 ${fmtK(contextUsage.context_window)}`
          : undefined}
      >
        <span title="当前会话使用的模型">{modelName || '未配置模型'}</span>{' '}
        <span className="composer-hint-sep">·</span>{' '}
        <span title={`模型配置中的 reasoning_effort：${(reasoningEffort || 'default').trim() || 'default'}`}>
          {(reasoningEffort || 'default').trim() || 'default'}
        </span>{' '}
        <span className="composer-hint-sep">·</span>{' '}
        {streaming ? (
          // The meter is a session-level snapshot refreshed after a turn; showing
          // it as a live figure while tokens stream in reads as a stalled number.
          <span className="hint-streaming" title="本轮回复结束后刷新上下文统计">
            {contextUsage && contextUsage.used_tokens > 0
              ? `${fmtK(contextUsage.used_tokens)} / ${fmtK(contextUsage.context_window)} · ${contextUsage.percent.toFixed(1)}% · `
              : ''}
            生成中…
          </span>
        ) : contextUsage ? (
          contextUsage.remaining_to_compact <= 0
            ? <>{fmtK(contextUsage.used_tokens)} / {fmtK(contextUsage.context_window)} · {contextUsage.percent.toFixed(1)}% · 即将自动压缩</>
            : <>{fmtK(contextUsage.used_tokens)} / {fmtK(contextUsage.context_window)} · {contextUsage.percent.toFixed(1)}% · 距压缩 {fmtK(contextUsage.remaining_to_compact)}</>
        ) : '上下文统计加载中…'}
      </div>
    </div>
  )
}
