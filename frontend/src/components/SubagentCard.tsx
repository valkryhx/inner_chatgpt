import { useState } from 'react'
import type { SubagentBlock, SubagentEventItem } from '../types'
import Markdown from './Markdown'

const ROLE_LABEL: Record<string, string> = {
  general: '通用',
}

const STATUS_LABEL: Record<string, string> = {
  running: '运行中',
  completed: '已完成',
  errored: '失败',
  stopped: '已停止',
  closed: '已关闭',
}

function flat(s: string, max = 100): string {
  const t = s.replace(/\s+/g, ' ').trim()
  return t.length > max ? t.slice(0, max - 1) + '…' : t
}

function summarizeInput(input: unknown): string {
  if (!input) return ''
  try {
    if (typeof input === 'string') return flat(input, 60)
    const o = input as Record<string, unknown>
    for (const k of ['path', 'file_path', 'command', 'query', 'skill', 'name']) {
      const v = o[k]
      if (typeof v === 'string' && v) return flat(v, 60)
    }
    return flat(JSON.stringify(input), 60)
  } catch {
    return ''
  }
}

/** 把连续 text/reasoning 合并，tool 事件单独成行 */
function groupEvents(events: SubagentEventItem[]): SubagentEventItem[] {
  const out: SubagentEventItem[] = []
  for (const ev of events) {
    const last = out[out.length - 1]
    if ((ev.kind === 'text' || ev.kind === 'reasoning') && last && last.kind === ev.kind) {
      last.text = (last.text || '') + (ev.text || '')
    } else {
      out.push({ ...ev })
    }
  }
  return out
}

export default function SubagentCard({ block }: { block: SubagentBlock }) {
  const [open, setOpen] = useState(false)
  const status = block.status || 'running'
  const running = status === 'running'
  const failed = status === 'errored'
  let events = groupEvents(block.events || [])

  // 去重：子代理的最终答复既会以 text 事件流出，也会作为 result 落到「结果」区，
  // 这里把与 result 完全一致的末尾文本事件去掉，避免同一段话出现两次。
  const resultText = (block.result || '').trim()
  if (resultText) {
    const lastText = [...events].reverse().find((e) => e.kind === 'text')
    if (lastText && (lastText.text || '').trim() === resultText) {
      events = events.filter((e) => e !== lastText)
    }
  }
  const toolCount = (block.events || []).filter((e) => e.kind === 'tool_start').length

  const meta = [
    ROLE_LABEL[block.role] || block.role || '通用',
    running ? '' : STATUS_LABEL[status] || status,
    toolCount ? `${toolCount} 个工具` : '',
    block.turns ? `${block.turns} 轮` : '',
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    <div className={'subagent-card ' + status}>
      <button type="button" className="subagent-head" onClick={() => setOpen(!open)}>
        <span className={'status-dot ' + (running ? 'running' : failed ? 'failed' : 'done')} />
        <span className="subagent-name">{block.name}</span>
        <span className="subagent-meta">{meta}</span>
        <span className="step-caret">{open ? '▾' : '▸'}</span>
      </button>

      {open && (
        <div className="subagent-body">
          {block.task && (
            <div className="subagent-task">
              <span className="step-label">任务</span>
              <div>{block.task}</div>
            </div>
          )}
          {events.length > 0 && (
            <div className="subagent-timeline">
              {events.map((ev, i) => {
                if (ev.kind === 'text') {
                  return (
                    <div className="subagent-text" key={i}>
                      <Markdown text={ev.text || ''} />
                    </div>
                  )
                }
                if (ev.kind === 'reasoning') {
                  return (
                    <div className="subagent-reasoning" key={i}>
                      {flat(ev.text || '', 200)}
                    </div>
                  )
                }
                const done = ev.kind === 'tool_result'
                return (
                  <div className="subagent-step" key={i}>
                    <span className={'status-dot ' + (done ? (ev.is_error ? 'failed' : 'done') : 'running')} />
                    <span className="step-name">{ev.name}</span>
                    {ev.kind === 'tool_start' && summarizeInput(ev.input) && (
                      <span className="step-args">{summarizeInput(ev.input)}</span>
                    )}
                  </div>
                )
              })}
            </div>
          )}
          {block.result && (
            <div className="subagent-result">
              <span className="step-label">结果</span>
              <Markdown text={block.result} />
            </div>
          )}
          {block.error && <div className="subagent-error">错误：{block.error}</div>}
        </div>
      )}
    </div>
  )
}
