import { useRef, useState } from 'react'
import type { ReactNode } from 'react'
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
  /** 模型选择器由 App 注入，位置在输入框下方的说明行右侧 */
  modelSelector?: ReactNode
  /** 思考等级选择器，紧挨在模型选择器后面 */
  effortSelector?: ReactNode
}

function fmtK(n: number): string {
  return `${(Math.max(0, n) / 1000).toFixed(1)}k`
}

const svgProps = {
  viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 1.8, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
  'aria-hidden': true,
}

function MicIcon() {
  return (
    <svg {...svgProps} width="16" height="16">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
    </svg>
  )
}

function WaveIcon() {
  return (
    <svg {...svgProps} width="16" height="16">
      <path d="M4 12h1M8 8v8M12 5v14M16 8v8M20 12h-1" />
    </svg>
  )
}

function ChevronIcon() {
  return (
    <svg {...svgProps} width="12" height="12" strokeWidth={2}>
      <path d="M6 9l6 6 6-6" />
    </svg>
  )
}

function SendIcon() {
  return (
    <svg {...svgProps} width="16" height="16" strokeWidth={2.2}>
      <path d="M12 19V5M6 11l6-6 6 6" />
    </svg>
  )
}

export default function Composer({
  streaming, onSend, onStop, onCompact, contextUsage, modelSelector, effortSelector,
}: Props) {
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
    if (uploading) return
    const t = text.trim()
    // 生成中：纯文本允许提交（作为插话），带附件仍不允许。
    if (streaming) {
      if (!t) return
      onSend(t, [])
      clear()
      return
    }
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

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(e.clipboardData?.files || [])
    if (files.some((f) => f.type.startsWith('image/'))) {
      e.preventDefault()
      addFiles(files)
    }
  }

  const autoGrow = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value)
    e.target.style.height = 'auto'
    e.target.style.height = Math.min(e.target.scrollHeight, 200) + 'px'
  }

  const canSend = !streaming && !uploading && (!!text.trim() || pending.length > 0)
  const ctxPct = contextUsage && contextUsage.context_window > 0
    ? ((contextUsage.used_tokens / contextUsage.context_window) * 100).toFixed(1)
    : ''

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
          {uploading ? '…' : '＋'}
        </button>
        <textarea
          ref={taRef}
          value={text}
          onChange={autoGrow}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          placeholder={
            pending.length
              ? '图片已就绪，直接发送或补充一句说明…'
              : streaming
                ? '补充一句要求…（会插入当前任务，不会另起一轮）'
                : '输入消息…'
          }
          rows={1}
          // 生成中也能输入：按发送会把这句话插进正在跑的回合（steering）。
          // 以前这里 disabled，用户以为程序卡死。
          disabled={uploading}
        />
        <div className="composer-right">
          {streaming ? (
            <>
              {!!text.trim() && (
                <button
                  className="send-icon"
                  onClick={submit}
                  title="插入当前任务（不另起一轮）"
                >
                  <SendIcon />
                </button>
              )}
              <button className="send-icon stop" onClick={onStop} title="停止生成">■</button>
            </>
          ) : canSend ? (
            <button className="send-icon" onClick={submit} title="发送（Enter）"><SendIcon /></button>
          ) : (
            <>
              <button className="icon-btn" disabled title="语音输入（未实现）" aria-label="语音输入">
                <MicIcon />
              </button>
              <button className="icon-btn chev" disabled title="更多（未实现）" aria-label="更多">
                <ChevronIcon />
              </button>
              <button className="icon-btn" disabled title="语音模式（未实现）" aria-label="使用语音模式">
                <WaveIcon />
              </button>
            </>
          )}
        </div>
      </div>
      <div className="composer-foot">
        <span className="foot-note">
          Hlaudz 可能会出错，请核对重要信息。
          {ctxPct && (
            <>
              {' · '}
              {fmtK(contextUsage!.used_tokens)} / {fmtK(contextUsage!.context_window)} · {ctxPct}%
            </>
          )}
        </span>
        <span className="foot-selectors">
          {modelSelector}
          {effortSelector}
        </span>
      </div>
    </div>
  )
}
