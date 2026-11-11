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
  /**
   * 从搜索结果跳转过来时，要定位的那条消息。
   * 只在 focusNonce 变化时生效一次，用户随后手动滚动不回弹。
   */
  focusMessageId?: string | null
  focusNonce?: number
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
    } else if (block.type === 'injected_message') {
      const note = (block as { type: 'injected_message'; text: string }).text || ''
      if (note) out.push(<InjectedNote key={'inj_' + i} text={note} />)
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
    if (b.type === 'injected_message') return !!(b as { text?: string }).text
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
    <div
      className={'msg ' + (isUser ? 'user' : 'assistant') + (msg.status === 'error' ? ' has-error' : '')}
      data-msg-id={msg.id}
    >
      <div className="bubble-col">
        {/* 生成中投递的插话：明确标注「已插入当前任务」，
            否则用户会以为它像普通消息一样另起了一轮。 */}
        {msg.status === 'queued' && (
          <div className="steer-badge" title="会插入正在执行的任务，不会另起一轮">
            已插入当前任务
          </div>
        )}
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
        {!isUser && !isActiveStream && (msg.content || hasVisibleBlocks) && <MessageActions msg={msg} />}
        {!isUser && <RunFooter msg={msg} live={isActiveStream} />}
      </div>
    </div>
  )
}


/** 平台注入消息的来源分类。
 *
 * 以前这里把标签写死成「团队消息」，但「注入」至少有四类来源，
 * 只有两类真来自 Agent Team。用户中途插话（steering）被显示成
 * 「团队消息」，正是这处写死造成的误解。按文本前缀判定来源。
 */
type InjectedKind = {
  label: string
  mark: string
  /** 去掉与标签重复的前缀，预览里不再看第二遍。 */
  strip: RegExp | null
}

const INJECTED_KINDS: Array<{ test: (t: string) => boolean; kind: InjectedKind }> = [
  {
    // steering：agent_loop 注入的用户中途补充（steer_queue.wrap_user_steer）
    test: (t) => t.startsWith('[用户在你执行过程中补充的消息]'),
    kind: {
      label: '你的补充',
      mark: '↩',
      strip: /^\[用户在你执行过程中补充的消息\]\s*/,
    },
  },
  {
    // 子代理完成通知（subagent/host.py 的 _notification_chunks）
    test: (t) => t.startsWith('[任务通知]'),
    kind: {
      label: '子代理完成',
      mark: '✓',
      strip: /^\[任务通知\]\s*/,
    },
  },
  {
    // teammate / lead 投递的团队消息（subagent/host.py 的 deliver_team_message）
    test: (t) => /^\[来自 [^\]]+ 的团队消息\]/.test(t),
    kind: {
      label: '团队消息',
      mark: '↩',
      strip: /^\[来自 [^\]]+ 的团队消息\]\s*/,
    },
  },
]

const INJECTED_FALLBACK: InjectedKind = { label: '平台提示', mark: '·', strip: null }

function classifyInjected(text: string): InjectedKind {
  for (const { test, kind } of INJECTED_KINDS) {
    if (test(text)) return kind
  }
  return INJECTED_FALLBACK
}

/** 平台注入到本轮的上下文消息：视觉上区别于用户发言与模型正文，可折叠。 */
function InjectedNote({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  const NEWLINE = String.fromCharCode(10)
  const kind = classifyInjected(text)
  // 标签已经说明了来源，预览里把重复的前缀去掉，避免同一句话看两遍。
  const body = kind.strip ? text.replace(kind.strip, '') : text
  const firstLine = body.split(NEWLINE)[0] || ''
  const multi = body.includes(NEWLINE) || body.length > firstLine.length
  return (
    <div className="injected-note">
      <button type="button" className="injected-note-head" onClick={() => setOpen((v) => !v)}
              aria-expanded={open}>
        <span className="injected-note-mark" aria-hidden="true">{kind.mark}</span>
        <span className="injected-note-label">{kind.label}</span>
        <span className="injected-note-preview">{firstLine}</span>
        {multi && <span className="injected-note-caret">{open ? '收起' : '展开'}</span>}
      </button>
      {open && multi && <p className="injected-note-body">{body}</p>}
    </div>
  )
}


/** 助手消息下方的操作行：复制可用，其余为占位（对齐 claude.ai 的位置）。 */
function ActionIcon({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
         strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  )
}

/** 实心图标（赞/踩）：轮廓取自 claude.ai 的 Anthropicons 图标字体，非描边。 */
function SolidIcon({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"
         fillRule="evenodd" clipRule="evenodd" aria-hidden="true">
      <path d={d} />
    </svg>
  )
}

const ICON_COPY = 'M9 9h9a1.5 1.5 0 0 1 1.5 1.5v9A1.5 1.5 0 0 1 18 21H9a1.5 1.5 0 0 1-1.5-1.5v-9A1.5 1.5 0 0 1 9 9ZM5.5 15H5a1.5 1.5 0 0 1-1.5-1.5v-9A1.5 1.5 0 0 1 5 3h9a1.5 1.5 0 0 1 1.5 1.5V5'
const ICON_SPEAK = 'M11 5 6.5 9H3v6h3.5L11 19V5ZM15 9a4 4 0 0 1 0 6'
/* 赞/踩的轮廓由 claude.ai 页面的图标字体（Anthropicons RVRN U+E0FB / U+E0F9）
 * 提取为 SVG path，再归一到 24x24 视口；字形为实心图形，故走 SolidIcon。 */
const ICON_UP = 'M18.14 8.83Q19.18 8.83 20.07 9.33Q20.96 9.83 21.58 10.68Q22.13 11.49 22.38 12.5Q22.62 13.51 22.39 14.51L21.42 18.84Q21.19 19.94 20.56 20.9Q19.92 21.86 19 22.48Q18.07 23.09 16.97 23.19Q14.69 23.42 12.37 23.17Q10.04 22.93 7.77 21.96H7.8Q7.38 21.79 7.09 21.73Q6.79 21.66 6.5 21.66L4.29 21.7Q3.19 21.6 2.39 20.8Q1.59 20.01 1.5 18.9V13.38L1.63 12.63Q1.85 11.88 2.39 11.33Q2.93 10.78 3.71 10.58L4.03 10.48L5.88 10.42Q6.11 10.39 6.27 10.16L9.1 2.85Q9.46 1.9 10.32 1.3Q11.18 0.7 12.22 0.7Q13.26 0.7 14.07 1.25Q14.89 1.81 15.31 2.68Q15.73 3.56 15.57 4.57L14.76 9.9Q14.82 9.48 14.55 9.15Q14.27 8.83 13.85 8.83ZM18.14 10.71H13.85Q13.39 10.71 13.12 10.39Q12.84 10.06 12.9 9.64L13.68 4.31Q13.78 3.63 13.34 3.11Q12.9 2.59 12.22 2.59Q11.77 2.59 11.39 2.86Q11.02 3.14 10.86 3.56L8.16 10.58L7.87 11.2Q7.25 12.11 6.24 12.3L4.84 12.34L4.23 12.4Q3.61 12.56 3.45 13.18L3.41 13.28V18.74Q3.45 19.16 3.74 19.45Q4.03 19.75 4.45 19.78H6.53Q6.99 19.78 7.46 19.88Q7.93 19.97 8.52 20.2H8.55Q10.47 21.05 12.53 21.29Q14.59 21.53 16.77 21.31Q17.81 21.21 18.58 20.35Q19.34 19.49 19.57 18.45L20.54 14.12Q20.67 13.51 20.53 12.89Q20.38 12.27 20.02 11.78Q19.7 11.33 19.21 11.02Q18.72 10.71 18.14 10.71Z'
const ICON_DOWN = 'M5.86 15.17Q4.82 15.17 3.93 14.67Q3.04 14.17 2.42 13.32Q1.87 12.51 1.62 11.5Q1.38 10.49 1.61 9.49L2.58 5.16Q2.81 4.06 3.44 3.1Q4.08 2.14 5 1.52Q5.93 0.91 7.03 0.81Q9.31 0.58 11.63 0.83Q13.96 1.07 16.23 2.04H16.2Q16.62 2.21 16.91 2.27Q17.21 2.34 17.5 2.34L19.71 2.3Q20.81 2.4 21.61 3.2Q22.41 3.99 22.5 5.1V10.62L22.37 11.37Q22.15 12.12 21.61 12.67Q21.07 13.22 20.29 13.42L19.94 13.48L18.12 13.58Q17.89 13.61 17.73 13.84L14.9 21.15Q14.54 22.1 13.68 22.7Q12.82 23.3 11.78 23.3Q10.74 23.3 9.93 22.75Q9.11 22.19 8.69 21.32Q8.27 20.44 8.43 19.43L9.24 14.07Q9.18 14.49 9.45 14.83Q9.73 15.17 10.15 15.17ZM5.86 13.26H10.15Q10.61 13.26 10.88 13.6Q11.16 13.94 11.1 14.36L10.32 19.69Q10.22 20.37 10.66 20.88Q11.1 21.38 11.78 21.38Q12.23 21.38 12.61 21.12Q12.98 20.86 13.14 20.44L15.84 13.42L16.13 12.8Q16.75 11.89 17.76 11.7L17.95 11.66L19.77 11.6Q20.39 11.44 20.55 10.82L20.59 10.72V5.26Q20.55 4.84 20.26 4.55Q19.97 4.25 19.55 4.22H17.47Q17.01 4.22 16.54 4.12Q16.07 4.03 15.48 3.8H15.45Q13.53 2.95 11.47 2.71Q9.41 2.47 7.23 2.69Q6.19 2.79 5.42 3.65Q4.66 4.51 4.43 5.55L3.46 9.88Q3.33 10.49 3.47 11.11Q3.62 11.73 3.98 12.22Q4.3 12.64 4.79 12.95Q5.28 13.26 5.86 13.26Z'
const ICON_RETRY = 'M4 12a8 8 0 0 1 13.7-5.7L20 8M20 4v4h-4M20 12a8 8 0 0 1-13.7 5.7L4 16M4 20v-4h4'

function MessageActions({ msg }: { msg: Message }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(msg.content || '')
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* 剪贴板不可用时静默失败 */
    }
  }
  return (
    <div className="msg-actions">
      <button className="icon-btn" title={copied ? '已复制' : '复制'} onClick={copy}>
        {copied ? '✓' : <ActionIcon d={ICON_COPY} />}
      </button>
      <button className="icon-btn" disabled title="朗读（未实现）"><ActionIcon d={ICON_SPEAK} /></button>
      <button className="icon-btn" disabled title="赞（未实现）"><SolidIcon d={ICON_UP} /></button>
      <button className="icon-btn" disabled title="踩（未实现）"><SolidIcon d={ICON_DOWN} /></button>
      <button className="icon-btn" disabled title="重试（未实现）"><ActionIcon d={ICON_RETRY} /></button>
    </div>
  )
}

/** 距离底部多少像素以内，仍视为“用户想跟随新内容”。 */
const FOLLOW_THRESHOLD_PX = 96

export default function MessageList({ messages, streaming, autoCompacted, sessionKey, sendPulse, onOpenWorkspace, focusMessageId, focusNonce }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const innerRef = useRef<HTMLDivElement>(null)
  const followRef = useRef(true)
  const frameRef = useRef<number | null>(null)
  const lastScrollTopRef = useRef(0)
  const programmaticUntilRef = useRef(0)
  // 已经定位过的 focusNonce，避免每次 render 重复滚动同一个目标。
  const focusAppliedRef = useRef<number | undefined>(undefined)

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

  // 从搜索结果跳进来时，滚到命中的那条消息而不是贴底。
  // 依赖 messages：切会话后历史是异步加载的，目标节点要等它到达才存在。
  useLayoutEffect(() => {
    if (!focusMessageId) return
    if (focusAppliedRef.current === focusNonce) return
    const el = scrollRef.current
    if (!el) return
    const node = el.querySelector<HTMLElement>(
      `[data-msg-id="${CSS.escape(focusMessageId)}"]`
    )
    if (!node) return
    // 定位意图优先于贴底：先关掉跟随，否则下一帧的贴底会把它拽回底部。
    followRef.current = false
    focusAppliedRef.current = focusNonce
    node.scrollIntoView({ block: 'center', behavior: 'auto' })
    lastScrollTopRef.current = el.scrollTop
    programmaticUntilRef.current = performance.now() + 200
    // 一次性高亮，让用户看清落到哪条上；动画结束自己褪掉。
    node.classList.add('msg-flash')
    window.setTimeout(() => node.classList.remove('msg-flash'), 1600)
  }, [focusMessageId, focusNonce, messages])

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
