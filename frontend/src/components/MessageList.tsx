import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { ReactNode, WheelEvent } from 'react'
import type { ChartBlock, Message, MessageBlock, SubagentBlock, SummaryBlock, ToolBlock } from '../types'
import ChartView from './ChartView'
import MarkdownImage from './MarkdownImage'
import Markdown from './Markdown'
import SubagentCard from './SubagentCard'
import { basename, downloadWorkspaceFile } from '../files'
import { toolState } from '../toolState'

interface Props {
  messages: Message[]
  streaming: boolean
  autoCompacted: number
  activeStreamMsgId?: string | null
  /** 会话切换时重置滚动跟随状态。 */
  sessionKey?: string | null
  /** 每次用户提交时自增；发送是明确的“看最新消息”意图。 */
  sendPulse?: number
  /** code_run 等步骤的「工作区」入口。 */
  onOpenWorkspace?: () => void
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

function StepRow({ block, interrupted, onOpenWorkspace }: {
  block: ToolBlock
  interrupted: boolean
  onOpenWorkspace?: () => void
}) {
  const [open, setOpen] = useState(false)
  const [dlBusy, setDlBusy] = useState(false)
  const [dlErr, setDlErr] = useState('')
  const state = toolState(block, interrupted)
  const argSummary = summarizeInput(block.input)
  const input = block.input as Record<string, unknown> | undefined
  const filePath = block.name === 'file_write' || block.name === 'file_patch'
    ? (typeof input?.path === 'string' ? input.path : '')
    : ''
  // 完成态只靠绿点表达，不再重复写“完成”，避免整列噪音
  const stateLabel = state === 'running' ? '运行中' : state === 'stopped' ? '已中断' : state === 'failed' ? '失败' : ''

  const download = async () => {
    if (!filePath || dlBusy) return
    setDlBusy(true)
    setDlErr('')
    try {
      await downloadWorkspaceFile(filePath, basename(filePath))
    } catch (err: unknown) {
      setDlErr(err instanceof Error ? err.message : String(err))
    } finally {
      setDlBusy(false)
    }
  }

  return (
    <div className={'step ' + state}>
      <span className={'status-dot ' + state} />
      <button type="button" className="step-main" onClick={() => setOpen(!open)}>
        <span className="step-name">{block.name}</span>
        {argSummary && <span className="step-args">{argSummary}</span>}
        {stateLabel && <span className="step-state">{stateLabel}</span>}
        <span className="step-caret">{open ? '▾' : '▸'}</span>
      </button>
      {filePath && (
        <button
          type="button"
          className="step-dl"
          onClick={(e) => { e.stopPropagation(); download() }}
          disabled={dlBusy}
          title={dlErr || `下载 ${filePath}`}
        >
          {dlBusy ? '…' : '下载'}
        </button>
      )}
      {block.name === 'code_run' && onOpenWorkspace && (
        <button
          type="button"
          className="step-dl"
          onClick={(e) => { e.stopPropagation(); onOpenWorkspace() }}
          title="打开工作区文件面板"
        >
          文件
        </button>
      )}
      {dlErr && <span className="step-dl-error" title={dlErr}>{dlErr}</span>}
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

function StepGroup({ blocks, interrupted, onOpenWorkspace }: {
  blocks: ToolBlock[]
  interrupted: boolean
  onOpenWorkspace?: () => void
}) {
  const states = blocks.map((b) => toolState(b, interrupted))
  const running = states.includes('running')
  const failed = states.includes('failed')
  const stopped = states.includes('stopped')
  const summary = running ? '运行中' : failed ? '有失败' : stopped ? '已中断' : '全部完成'
  return (
    <div className="step-group">
      {blocks.length > 1 && (
        <div className="step-group-head">
          <span>{blocks.length} 个步骤</span>
          <span className={'step-group-sum ' + (running ? 'running' : failed ? 'failed' : stopped ? 'stopped' : 'done')}>{summary}</span>
        </div>
      )}
      {blocks.map((b, i) => (
        <StepRow key={b.id || 'tool_' + i} block={b} interrupted={interrupted} onOpenWorkspace={onOpenWorkspace} />
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
  // Mounted while live: keep the body open after the phase changes to tool/text.
  // Historical completed blocks still mount with live=false and stay collapsed.
  const [open, setOpen] = useState(live)
  const chars = text.length
  return (
    <div className={'reasoning-block' + (live ? ' live' : '')}>
      <button type="button" className="reasoning-row" onClick={() => setOpen(!open)}>
        <span className={'status-dot ' + (live ? 'running' : 'done')} />
        <span className="reasoning-title">{live ? '思考中' : '思考'}</span>
        <span className="reasoning-meta">{fmtChars(chars)}</span>
        <span className="step-caret">{open ? '▾' : '▸'}</span>
      </button>
      {open && <div className="reasoning-body">{text}</div>}
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

function renderBlocks(
  blocks: MessageBlock[],
  isUser: boolean,
  streaming: boolean,
  interrupted: boolean,
  onImageSettled: () => void,
  onOpenWorkspace?: () => void,
): ReactNode[] {
  const out: ReactNode[] = []
  // 只有“最后一个块 + 消息仍在流式”才算真正进行中；
  // 否则同一轮里较早的思考块会被误判为 live，凭空长出橘色光标。
  const liveIdx = streaming ? blocks.length - 1 : -1
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
      out.push(<StepGroup key={'tg_' + i} blocks={group} interrupted={interrupted} onOpenWorkspace={onOpenWorkspace} />)
      i = j
      continue
    }
    if (block.type === 'attachments') {
      const items = (block as { type: 'attachments'; items: { path: string; name?: string }[] }).items || []
      if (items.length) {
        out.push(
          <div className="msg-attachments" key={'att_' + i}>
            {items.map((it, k) => (
              <MarkdownImage
                key={'att_' + i + '_' + k}
                src={it.path}
                alt={it.name || '图片'}
                onSettled={() => onImageSettled?.()}
              />
            ))}
          </div>,
        )
      }
    } else if (block.type === 'summary') {
      out.push(<SummaryCard key={'sum_' + i} block={block} />)
    } else if (block.type === 'reasoning') {
      out.push(<ReasoningSection key={'reason_' + i} text={block.text} live={i === liveIdx} />)
    } else if (block.type === 'chart') {
      out.push(<ChartView key={'chart_' + i} spec={block.spec} />)
    } else if (block.type === 'subagent') {
      const sb = block as SubagentBlock
      // 停止/断连时父 SSE 可能来不及转发子代理的 subagent_end；消息已不在流式时
      // 不能再把卡片显示成“运行中”（旧版本落库的数据也走这条兜底）。
      const shown: SubagentBlock = sb.status === 'running' && !streaming
        ? { ...sb, status: 'stopped' }
        : sb
      out.push(<SubagentCard key={'sub_' + i + '_' + sb.agent_id} block={shown} />)
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
              <Markdown text={text} onImageSettled={onImageSettled} />
            </div>,
          )
        }
      }
    }
    i++
  }
  return out
}

function formatRunDuration(ms: number): string {
  const seconds = Math.max(0, ms) / 1000
  if (seconds < 10) return seconds.toFixed(1) + 's'
  return Math.round(seconds) + 's'
}

function RunFooter({ msg, live }: { msg: Message; live: boolean }) {
  const started = msg.run_started_at || msg.created_at
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    if (!live) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 100)
    return () => window.clearInterval(timer)
  }, [live, started])

  const finished = msg.run_finished_at
  if (live) {
    return (
      <div className="run-footer is-live" aria-live="polite">
        <span className="run-footer-mark" />
        <span>Working</span>
        <span className="run-footer-dot">·</span>
        <span className="run-footer-time">{formatRunDuration(now - started)}</span>
      </div>
    )
  }
  if (!finished) return null

  const label = msg.status === 'stopped' ? 'Stopped' : msg.status === 'error' ? 'Failed' : 'Worked'
  return (
    <div className={'run-footer ' + (msg.status === 'error' ? 'is-error' : msg.status === 'stopped' ? 'is-stopped' : 'is-done')}>
      <span>{label}</span>
      <span className="run-footer-dot">·</span>
      <span className="run-footer-time">{formatRunDuration(finished - started)}</span>
      {msg.status === 'complete' && (
        <>
          <span className="run-footer-dot">·</span>
          <span className="run-footer-hint">可以继续输入</span>
        </>
      )}
    </div>
  )
}

function MessageRow({ msg, onImageSettled, onOpenWorkspace }: {
  msg: Message
  onImageSettled: () => void
  onOpenWorkspace?: () => void
}) {
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
  const hasVisibleBlocks = blocks.some((b) => {
    if (b.type === 'text' || b.type === 'reasoning') return !!b.text
    if (b.type === 'tool') return true
    if (b.type === 'chart' || b.type === 'summary') return true
    if (b.type === 'attachments') return !!((b as { items?: unknown[] }).items || []).length
    if (b.type === 'subagent') {
      const s = b as SubagentBlock
      return !!(s.task || s.status || s.result || (s.events && s.events.length))
    }
    return false
  })
  // 空态判断必须覆盖所有可见块类型，不能只看 text/content；
  // stopped 用单独的结束提示，避免同时闪烁“可能已中断”和“已停止”。
  const hasVisibleContent = !!msg.content || hasReasoning || hasVisibleBlocks
  const isEmptyNoLive =
    !isUser &&
    msg.status !== 'streaming' &&
    msg.status !== 'stopped' &&
    !hasVisibleContent
  return (
    <div className={'msg ' + (isUser ? 'user' : 'assistant') + (msg.status === 'error' ? ' has-error' : '')}>
      <div className="bubble-col">
        {hasReasoning && !hasReasoningBlocks && <ReasoningSection text={msg.reasoning} live={isActiveStream} />}
        {renderBlocks(blocks, isUser, isActiveStream, !isActiveStream, onImageSettled, onOpenWorkspace)}
        {!hasTextBlocks && msg.content && (
          isUser ? (
            <div className="bubble">{msg.content}</div>
          ) : (
            <div className="answer">
              <Markdown text={msg.content} onImageSettled={onImageSettled} />
            </div>
          )
        )}
        {/* 已有 live 思考块/思考内容时不再追加一个「正在思考」，避免同一状态重复出现 */}
        {isActiveStream && !msg.content && !hasReasoning && !hasReasoningBlocks && chartBlocks.length === 0 && (
          <div className="typing">正在思考</div>
        )}
        {isEmptyNoLive && (
          <div className="session-meta stale-note">
            {msg.status === 'error' ? '（生成失败）' : '（未生成回复，可能已中断）'}
          </div>
        )}
        {!isUser && <RunFooter msg={msg} live={isActiveStream} />}
      </div>
    </div>
  )
}

/** 距离底部多少像素以内，仍视为“用户想跟随新内容”。 */
const FOLLOW_THRESHOLD_PX = 96

export default function MessageList({ messages, streaming, autoCompacted, sessionKey, sendPulse, onOpenWorkspace }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const innerRef = useRef<HTMLDivElement>(null)
  const followRef = useRef(true)
  const frameRef = useRef<number | null>(null)
  const lastScrollTopRef = useRef(0)
  const programmaticUntilRef = useRef(0)

  const pinToBottom = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    // 流式阶段必须瞬时贴底；平滑滚动追不上 token 增量，也会与图片高度变化打架。
    el.scrollTop = el.scrollHeight
    lastScrollTopRef.current = el.scrollTop
    programmaticUntilRef.current = performance.now() + 100
  }, [])

  const schedulePinToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    if (frameRef.current !== null) return
    programmaticUntilRef.current = performance.now() + (behavior === 'smooth' ? 500 : 100)
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = null
      const el = scrollRef.current
      if (!el) return
      if (behavior === 'smooth') el.scrollTo({ top: el.scrollHeight, behavior })
      else el.scrollTop = el.scrollHeight
      lastScrollTopRef.current = el.scrollTop
    })
  }, [])

  const isNearBottom = useCallback(() => {
    const el = scrollRef.current
    if (!el) return true
    return el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_THRESHOLD_PX
  }, [])

  const onScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const top = el.scrollTop
    const movedUp = top + 1 < lastScrollTopRef.current
    lastScrollTopRef.current = top

    // 用户滚轮上翻的意图必须在程序滚动保护之前记录，否则无法浏览历史。
    if (movedUp) {
      followRef.current = false
      return
    }
    if (performance.now() < programmaticUntilRef.current) {
      if (isNearBottom()) followRef.current = true
      return
    }
    // 用户主动滚回底部后恢复跟随；这比只看“是否发生上滚”更符合预期。
    followRef.current = isNearBottom()
  }, [isNearBottom])

  const onWheel = useCallback((event: WheelEvent<HTMLDivElement>) => {
    if (event.deltaY < 0) followRef.current = false
  }, [])

  // 切换会话后重新默认跟随底部，不能继承上一个会话的浏览位置。
  useLayoutEffect(() => {
    followRef.current = true
    lastScrollTopRef.current = 0
    schedulePinToBottom('auto')
  }, [sessionKey, schedulePinToBottom])

  // 用户按发送是比“正在浏览历史”更高优先级的意图：立即恢复跟随并贴底，
  // 保证乐观插入的用户消息第一眼就能看到，而不是停留在旧的历史位置。
  useLayoutEffect(() => {
    if (!sendPulse) return
    followRef.current = true
    schedulePinToBottom('auto')
  }, [sendPulse, schedulePinToBottom])

  // 每次 DOM 提交后、绘制前贴底。流式阶段每个 delta 都执行，
  // 图片/长文本撑开高度也发生在同一轮布局中，避免下一 tick 才补滚。
  useLayoutEffect(() => {
    if (!followRef.current) return
    if (streaming) pinToBottom()
    else schedulePinToBottom('auto')
  }, [messages, streaming, pinToBottom, schedulePinToBottom])

  // 异步图片尺寸（尤其 blob 图片）可能在 React 提交之外改变高度。
  // MutationObserver 捕获 Markdown 节点替换，ResizeObserver 捕获图片撑高；
  // 两者都在下一帧前同步贴底，避免流式 token 与图片加载竞争。
  const hasList = messages.length > 0 || streaming
  useEffect(() => {
    if (!hasList) return
    const inner = innerRef.current
    const el = scrollRef.current
    if (!inner || !el) return
    const syncBottom = () => {
      if (!followRef.current) return
      el.scrollTop = el.scrollHeight
      lastScrollTopRef.current = el.scrollTop
    }
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(syncBottom) : null
    ro?.observe(inner)
    const mo = typeof MutationObserver !== 'undefined' ? new MutationObserver(syncBottom) : null
    mo?.observe(inner, { childList: true, subtree: true, characterData: true })
    const onLoad = (event: Event) => {
      if ((event.target as Element)?.tagName === 'IMG') syncBottom()
    }
    inner.addEventListener('load', onLoad, true)
    inner.addEventListener('error', onLoad, true)
    return () => {
      ro?.disconnect()
      mo?.disconnect()
      inner.removeEventListener('load', onLoad, true)
      inner.removeEventListener('error', onLoad, true)
    }
  }, [hasList])

  const handleImageSettled = useCallback(() => {
    if (!followRef.current) return
    requestAnimationFrame(() => {
      if (followRef.current) pinToBottom()
    })
  }, [pinToBottom])

  useEffect(() => {
    return () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
    }
  }, [])

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
    <div className="messages" ref={scrollRef} onScroll={onScroll} onWheel={onWheel}>
      <div className="messages-inner" ref={innerRef}>
        {autoCompacted > 0 && (
          <div className="compaction-banner">上下文已压缩 {autoCompacted} 次（较早对话已摘要）</div>
        )}
        {messages.map((m) => (
          <MessageRow key={m.id} msg={m} onImageSettled={handleImageSettled} onOpenWorkspace={onOpenWorkspace} />
        ))}
      </div>
    </div>
  )
}
