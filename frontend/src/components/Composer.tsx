import { useRef, useState } from 'react'
import type { ContextUsage, UploadedImage } from '../types'
import { uploadImage } from '../api'
import MarkdownImage from './MarkdownImage'

interface Props {
  streaming: boolean
  onSend: (text: string, attachments?: Array<{ path: string; name?: string; mime?: string; size?: number }>) => void
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
  const [pending, setPending] = useState<UploadedImage[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const taRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const clear = () => {
    setText('')
    setPending([])
    if (taRef.current) taRef.current.style.height = 'auto'
  }

  // 选图后立刻上传，拿到稳定引用；发送时只带引用，不重传二进制。
  const addFiles = async (files: FileList | File[] | null) => {
    if (!files) return
    const picked = Array.from(files).filter((f) => f.type.startsWith('image/'))
    if (picked.length === 0) return
    setUploading(true)
    setUploadError('')
    try {
      for (const file of picked) {
        const up = await uploadImage(file)
        setPending((prev) => (prev.length >= 4 ? prev : [...prev, up]))
      }
    } catch (e: any) {
      setUploadError(e?.message || String(e))
    } finally {
      setUploading(false)
    }
  }

  const submit = () => {
    if (streaming || uploading) return
    const t = text.trim()
    if (!t && pending.length === 0) return
    if (!t) {
      // 纯图片消息：给模型一个明确的事由，避免只发一张图没有指令。
      onSend('请描述这张图片。', pending.map(({ path, name, mime, size }) => ({ path, name, mime, size })))
      clear()
      return
    }
    // 输入框指令：/compact [自定义要求] —— 手动压缩上下文，不作为普通消息发送
    const m = t.match(/^\/compact\s*([\s\S]*)$/i)
    if (m) {
      onCompact?.(m[1].trim())
      clear()
      return
    }
    onSend(t, pending.map(({ path, name, mime, size }) => ({ path, name, mime, size })))
    clear()
  }

  const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(e.clipboardData?.files || [])
    if (files.some((f) => f.type.startsWith('image/'))) {
      e.preventDefault()
      addFiles(files)
    }
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
      {uploadError && <div className="auth-error">{uploadError}</div>}
      {pending.length > 0 && (
        <div className="composer-attachments">
          {pending.map((f) => (
            <div className="composer-attachment" key={f.path}>
              <MarkdownImage src={f.path} alt={f.name || '图片'} />
              <button
                className="att-remove"
                title="移除"
                onClick={() => setPending((prev) => prev.filter((x) => x.path !== f.path))}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
      <div
        className="composer"
        onDragOver={(e) => { e.preventDefault() }}
        onDrop={(e) => { e.preventDefault(); addFiles(e.dataTransfer?.files || null) }}
      >
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          multiple
          hidden
          onChange={(e) => { addFiles(e.target.files); e.currentTarget.value = '' }}
        />
        <button
          className="attach-btn"
          title="添加图片（也可以直接粘贴或拖进来）"
          onClick={() => fileRef.current?.click()}
          disabled={streaming || uploading}
        >
          {uploading ? '…' : '+'}
        </button>
        <textarea
          ref={taRef}
          value={text}
          onChange={autoGrow}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          placeholder="输入消息，Enter 发送，Shift+Enter 换行；/compact 手动压缩上下文"
          rows={1}
          disabled={streaming}
        />
        {streaming ? (
          <button className="send-btn stop" onClick={onStop}>
            停止
          </button>
        ) : (
          <button className="send-btn" onClick={submit} disabled={(!text.trim() && pending.length === 0) || uploading}>
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
