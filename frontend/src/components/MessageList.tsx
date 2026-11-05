import { useEffect, useRef, useState } from 'react'
import type { ChartBlock, Message, MessageBlock, ReasoningBlock, SummaryBlock, ToolBlock } from '../types'
import ChartView from './ChartView'
import Markdown from './Markdown'

interface Props {
  messages: Message[]
  streaming: boolean
  autoCompacted: number
  activeStreamMsgId?: string | null
}

function ToolCard({ block }: { block: ToolBlock }) {
  const [open, setOpen] = useState(true)
  return (
    <div className="tool-card">
      <div className="tool-head" onClick={() => setOpen(!open)} style={{ cursor: 'pointer' }}>
        <span>🔧 {block.name}</span>
        <span style={{ marginLeft: 'auto', color: 'var(--muted)', fontWeight: 400 }}>
          {open ? '收起' : '展开'}
        </span>
      </div>
      {open && block.input !== undefined && (
        <div className="tool-args">参数: {JSON.stringify(block.input)}</div>
      )}
      {open && block.result !== undefined && <div className="tool-result">结果: {block.result}</div>}
      {open && block.result === undefined && <div className="tool-args">执行中…</div>}
    </div>
  )
}

function ReasoningSection({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(true)
  if (live) {
    return (
      <div className="reasoning live">
        <div className="reasoning-head">思考中…</div>
        <div className="reasoning-body">{text}</div>
      </div>
    )
  }
  return (
    <details
      className="reasoning"
      open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>思考过程（点击{open ? '收起' : '展开'}）</summary>
      <div className="reasoning-body">{text}</div>
    </details>
  )
}

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
      <div className="summary-head" onClick={() => setOpen(!open)} style={{ cursor: 'pointer' }}>
        <span>🗜️ 上下文已压缩</span>
        <span className="summary-meta">{meta}</span>
        <span style={{ marginLeft: 'auto', color: 'var(--muted)', fontWeight: 400 }}>{open ? '收起' : '展开摘要'}</span>
      </div>
      {open && <div className="summary-body">{block.text || '（无摘要正文）'}</div>}
    </div>
  )
}

function renderBlock(
  block: MessageBlock,
  idx: number,
  isUser: boolean,
  statusClass: string,
  live: boolean,
) {
  if (block.type === 'summary') return <SummaryCard key={'sum_' + idx} block={block} />
  if (block.type === 'reasoning') return <ReasoningSection key={'reason_' + idx} text={block.text} live={live} />
  if (block.type === 'chart') return <ChartView key={'chart_' + idx} spec={block.spec} />
  if (block.type === 'tool') return <ToolCard key={block.id || 'tool_' + idx} block={block} />
  if (block.type === 'text') {
    // 文本块必须按它在 blocks 里的位置渲染：工具调用前后的旁白是不同气泡，
    // 不能把整轮文本拼成一个（否则工具前的说明会被并进最终答案开头）。
    const text = block.text || ''
    if (!text) return null
    return (
      <div className={'bubble' + statusClass} key={'text_' + idx}>
        {isUser ? text : <Markdown text={text} />}
      </div>
    )
  }
  return null
}

function MessageRow({ msg, activeStreamMsgId }: { msg: Message; activeStreamMsgId?: string | null }) {
  const isUser = msg.role === 'user'
  const toolBlocks = (msg.blocks || []).filter((b): b is ToolBlock => b.type === 'tool')
  const summaryBlocks = (msg.blocks || []).filter((b): b is SummaryBlock => b.type === 'summary')
  const chartBlocks = (msg.blocks || []).filter((b): b is ChartBlock => b.type === 'chart')
  // blocks 里已有 text 块时，文本由 blocks 按位置渲染；msg.content 只作为旧数据兜底，
  // 否则会出现「旁白 + 最终答案」被拼成一个气泡（历史 bug）。
  const hasTextBlocks = (msg.blocks || []).some((b) => b.type === 'text')
  // 只要 blocks 里已经有 reasoning 块（新格式），就按事件顺序就地渲染，
  // 不再把整段思考额外追加到消息末尾（那正是“思考总在最后”的旧 bug）。
  const hasReasoningBlocks = (msg.blocks || []).some((b) => b.type === 'reasoning')
  const statusClass = msg.status === 'error' ? ' error' : msg.status === 'stopped' ? ' stopped' : ''
  // 判定是否“正在生成”：只要消息自身仍是 streaming，就说明是本端进行中的这一轮
  // （服务器历史已被后端收敛，不会返回遗留 streaming）。不要用 id 比对做判定，
  // 否则在流结束到重新加载之间的空档会误判成“已中断”而闪现提示。
  const isActiveStream = msg.status === 'streaming'
  const hasReasoning = !!msg.reasoning
  // 只有“非生成中且无内容、无工具”的 assistant 消息，才视为真正中断/失败。
  const isEmptyNoLive =
    !isUser &&
    msg.status !== 'streaming' &&
    !msg.content &&
    !hasReasoning &&
    toolBlocks.length === 0 &&
    summaryBlocks.length === 0 &&
    chartBlocks.length === 0
  return (
    <div className={'msg ' + (isUser ? 'user' : 'assistant')}>
      <div className={'avatar ' + (isUser ? 'user' : 'assistant')}>{isUser ? '你' : 'AI'}</div>
      <div className="bubble-col">
        {hasReasoning && !hasReasoningBlocks && (
          <ReasoningSection text={msg.reasoning} live={isActiveStream} />
        )}
        {(msg.blocks || []).map((b, i) =>
          renderBlock(b, i, isUser, statusClass, isActiveStream && i === (msg.blocks || []).length - 1),
        )}
        {!hasTextBlocks && msg.content && (
          <div className={'bubble' + statusClass}>
            {isUser ? msg.content : <Markdown text={msg.content} />}
          </div>
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

export default function MessageList({ messages, streaming, autoCompacted, activeStreamMsgId }: Props) {
  const endRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streaming])

  if (messages.length === 0 && !streaming) {
    return (
      <div className="messages">
        <div className="empty">
          <h2>开始对话</h2>
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
          <MessageRow key={m.id} msg={m} activeStreamMsgId={activeStreamMsgId} />
        ))}
        <div ref={endRef} />
      </div>
    </div>
  )
}
