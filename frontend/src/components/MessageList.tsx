import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { ChartBlock, Message, MessageBlock, SummaryBlock, ToolBlock } from '../types'
import ChartView from './ChartView'
import Markdown from './Markdown'

interface Props {
  messages: Message[]
  streaming: boolean
  autoCompacted: number
  activeStreamMsgId?: string | null
}

/* ---------- 工具步骤行（L2 过程层）：单行 + 状态点 + 点击展开 ---------- */

function summarizeInput(input: unknown): string {
  if (input === undefined || input === null) return ''
  try {
    if (typeof input === 'string') return truncate(input)
    if (typeof input === 'object') {
      const obj = input as Record<string, unknown>
      for (const k of ['path', 'file_path', 'command', 'cmd', 'query', 'name', 'skill', 'url', 'pattern']) {
        const v = obj[k]
        if (typeof v === 'string' && v) return truncate(v)
      }
      return truncate(JSON.stringify(input))
    }
    return truncate(String(input))
  } catch {
    return ''
  }
}

function truncate(s: string, max = 90): string {
  const flat = s.replace(/\s+/g, ' ').trim()
  return flat.length > max ? flat.slice(0, max - 1) + '…' : flat
}

function prettyJson(input: unknown): string {
  try {
    return typeof input === 'string' ? input : JSON.stringify(input, null, 2)
  } catch {
    return String(input)
  }
}

function toolState(block: ToolBlock): 'running' | 'failed' | 'done' {
  if (block.result === undefined) return 'running'
  if (block.result.startsWith('[工具执行错误]')) return 'failed'
  return 'done'
}

function StepRow({ block }: { block: ToolBlock }) {
  const [open, setOpen] = useState(false)
  const state = toolState(block)
  const argSummary = summarizeInput(block.input)
  // 完成态只靠绿点表达，不再重复写“完成”，避免整列噪音
  const stateLabel = state === 'running' ? '运行中' : state === 'failed' ? '失败' : ''
  return (
    <div className={'step ' + state}>
      <span className={'status-dot ' + state} />
      <button type="button" className="step-main" onClick={() => setOpen(!open)}>
        <span className="step-name">{block.name}</span>
        {argSummary && <span className="step-args">{argSummary}</span>}
        {stateLabel && <span className="step-state">{stateLabel}</span>}
        <span className="step-caret">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="step-detail">
          {block.input !== undefined && (
            <div className="step-section">
              <span className="step-label">参数</span>
              <pre>{prettyJson(block.input)}</pre>
            </div>
          )}
          {block.result !== undefined ? (
            <div className="step-section">
              <span className="step-label">结果</span>
              <pre>{block.result}</pre>
            </div>
          ) : (
            <div className="step-section">
              <span className="step-label">结果</span>
              <pre className="muted">执行中…</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function StepGroup({ blocks }: { blocks: ToolBlock[] }) {
  const states = blocks.map(toolState)
  const running = states.includes('running')
  const failed = states.includes('failed')
  const summary = running ? '运行中' : failed ? '有失败' : '全部完成'
  return (
    <div className="step-group">
      {blocks.length > 1 && (
        <div className="step-group-head">
          <span>{blocks.length} 个步骤</span>
          <span className={'step-group-sum ' + (running ? 'running' : failed ? 'failed' : 'done')}>{summary}</span>
        </div>
      )}
      {blocks.map((b, i) => (
        <StepRow key={b.id || 'tool_' + i} block={b} />
      ))}
    </div>
  )
}

/* ---------- 思考（L2 过程层）：默认折叠成一行 ---------- */

function fmtChars(n: number): string {
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, '') + 'k 字'
  return n + ' 字'
}

function ReasoningSection({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(false)
  const chars = text.length
  return (
    <div className={'reasoning-block' + (live ? ' live' : '')}>
      <button type="button" className="reasoning-row" onClick={() => setOpen(!open)}>
        <span className={'status-dot ' + (live ? 'running' : 'done')} />
        <span className="reasoning-title">{live ? '思考中' : '思考'}</span>
        <span className="reasoning-meta">{fmtChars(chars)}</span>
        <span className="step-caret">{open ? '▾' : '▸'}</span>
      </button>
      {(open || live) && <div className="reasoning-body">{text}</div>}
    </div>
  )
}

/* ---------- 上下文压缩摘要（L2）：细胶囊 ---------- */

function SummaryCard({ block }: { block: SummaryBlock }) {
  const [open, setOpen] = useState(false)
  const triggerLabel = block.trigger === 'hard_trim' ? '硬线裁剪' : block.trigger === 'auto' ? '自动摘要' : '手动压缩'
  const meta = [
    triggerLabel,
    block.summary_tokens != null ? `摘要 ${block.summary_tokens} tokens` : '',
    block.kept_messages != null ? `保留 ${block.kept_messages} 条` : '',
    block.dropped_messages ? `压缩 ${block.dropped_messages} 条` : '',
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <div className="summary-card">
      <button type="button" className="summary-head" onClick={() => setOpen(!open)}>
        <span className="summary-title">上下文已压缩</span>
        <span className="summary-meta">{meta}</span>
        <span className="step-caret">{open ? '▾' : '▸'}</span>
      </button>
      {open && <div className="summary-body">{block.text || '（无摘要正文）'}</div>}
    </div>
  )
}

/* ---------- 按 blocks 顺序渲染（连续 tool 合并成组，但绝不跨 text/reasoning 重排） ---------- */

function renderBlocks(blocks: MessageBlock[], isUser: boolean, live: boolean): ReactNode[] {
  const out: ReactNode[] = []
  let i = 0
  while (i < blocks.length) {
    const block = blocks[i]
    if (block.type === 'tool') {
      const group: ToolBlock[] = [block]
      let j = i + 1
      while (j < blocks.length && blocks[j].type === 'tool') {
        group.push(blocks[j] as ToolBlock)
        j++
      }
      out.push(<StepGroup key={'tg_' + i} blocks={group} />)
      i = j
      continue
    }
    if (block.type === 'summary') {
      out.push(<SummaryCard key={'sum_' + i} block={block} />)
    } else if (block.type === 'reasoning') {
      out.push(<ReasoningSection key={'reason_' + i} text={block.text} live={live} />)
    } else if (block.type === 'chart') {
      out.push(<ChartView key={'chart_' + i} spec={block.spec} />)
    } else if (block.type === 'text') {
      const text = block.text || ''
      if (text) {
        if (isUser) {
          out.push(
            <div className="bubble" key={'text_' + i}>
              {text}
            </div>,
          )
        } else {
          out.push(
            <div className="answer" key={'text_' + i}>
              <Markdown text={text} />
            </div>,
          )
        }
      }
    }
    i++
  }
  return out
}

function MessageRow({ msg }: { msg: Message }) {
  const isUser = msg.role === 'user'
  const blocks = msg.blocks || []
  const toolBlocks = blocks.filter((b): b is ToolBlock => b.type === 'tool')
  const summaryBlocks = blocks.filter((b): b is SummaryBlock => b.type === 'summary')
  const chartBlocks = blocks.filter((b): b is ChartBlock => b.type === 'chart')
  // blocks 里已有 text 块时按位置渲染；msg.content 只作为旧数据兜底，
  // 否则「旁白 + 最终答案」会被拼成一段（历史 bug）。
  const hasTextBlocks = blocks.some((b) => b.type === 'text')
  // 新格式的 reasoning 块已按事件顺序存在 blocks 中，就地渲染；
  // 旧的整段 msg.reasoning 才回退到 blocks 之前（修复“思考总在最后”）。
  const hasReasoningBlocks = blocks.some((b) => b.type === 'reasoning')
  const isActiveStream = msg.status === 'streaming'
  const hasReasoning = !!msg.reasoning
  const isEmptyNoLive =
    !isUser &&
    msg.status !== 'streaming' &&
    !msg.content &&
    !hasReasoning &&
    toolBlocks.length === 0 &&
    summaryBlocks.length === 0 &&
    chartBlocks.length === 0
  return (
    <div className={'msg ' + (isUser ? 'user' : 'assistant') + (msg.status === 'error' ? ' has-error' : '')}>
      <div className="bubble-col">
        {hasReasoning && !hasReasoningBlocks && <ReasoningSection text={msg.reasoning} live={isActiveStream} />}
        {renderBlocks(blocks, isUser, isActiveStream)}
        {!hasTextBlocks && msg.content && (
          isUser ? (
            <div className="bubble">{msg.content}</div>
          ) : (
            <div className="answer">
              <Markdown text={msg.content} />
            </div>
          )
        )}
        {isActiveStream && !msg.content && !hasReasoning && chartBlocks.length === 0 && (
          <div className="typing">正在思考</div>
        )}
        {isEmptyNoLive && (
          <div className="session-meta stale-note">
            {msg.status === 'error' ? '（生成失败）' : '（未生成回复，可能已中断）'}
          </div>
        )}
        {msg.status === 'stopped' && msg.content && <div className="session-meta">（已停止）</div>}
      </div>
    </div>
  )
}

export default function MessageList({ messages, streaming, autoCompacted }: Props) {
  const endRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streaming])

  if (messages.length === 0 && !streaming) {
    return (
      <div className="messages">
        <div className="empty">
          <h2>今天想让我做什么？</h2>
          <div>自研 Agent 内核驱动的对话助手，支持多轮、上下文压缩、会话 resume 与 MCP 工具。</div>
          <div style={{ fontSize: 12 }}>试试问：“现在几点？顺便算 128*37”</div>
        </div>
      </div>
    )
  }

  return (
    <div className="messages">
      <div className="messages-inner">
        {autoCompacted > 0 && (
          <div className="compaction-banner">上下文已压缩 {autoCompacted} 次（较早对话已摘要）</div>
        )}
        {messages.map((m) => (
          <MessageRow key={m.id} msg={m} />
        ))}
        <div ref={endRef} />
      </div>
    </div>
  )
}
