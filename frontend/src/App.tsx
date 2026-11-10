import { useCallback, useEffect, useRef, useState } from 'react'
import { api, getToken, setToken, streamMessage } from './api'
import type { Attachment, ContextUsage, LlmModelOption, McpServerStatus, Message, Session, StreamEvent, TeamEvent, TeamSnapshot, User } from './types'
import Sidebar from './components/Sidebar'
import MessageList from './components/MessageList'
import ModelMenu from './components/ModelMenu'
import ReasoningMenu from './components/ReasoningMenu'
import SessionBrowser from './components/SessionBrowser'
import Composer from './components/Composer'
import LoginPage from './components/LoginPage'
import SettingsModal from './components/SettingsModal'
import { mergeStart, mergeStatus } from './subagentView'
import TeamPanel from './components/TeamPanel'
import WorkspacePanel from './components/WorkspacePanel'

// 面板展开状态按用户隔离：同一浏览器换账号登录时不应继承上一个人的偏好。
const TEAM_OPEN_KEY = 'team_panel_open'

function newGenId() {
  return 'g_' + Math.random().toString(16).slice(2, 10)
}

function teamOpenKey(userId?: string | null): string {
  return userId ? `${TEAM_OPEN_KEY}:${userId}` : TEAM_OPEN_KEY
}

function readTeamOpen(userId?: string | null): boolean {
  try {
    return localStorage.getItem(teamOpenKey(userId)) === '1'
  } catch {
    return false
  }
}

// 消息块严格按事件发生顺序累积：文本增量并入末尾文本块，工具/图表插入当前位置。
function appendTextBlock(blocks: any[], chunk: string): any[] {
  if (!chunk) return blocks
  const last = blocks[blocks.length - 1]
  if (last && last.type === 'text') {
    return [...blocks.slice(0, -1), { ...last, text: last.text + chunk }]
  }
  return [...blocks, { type: 'text', text: chunk }]
}

function appendBlock(blocks: any[], block: any): any[] {
  return [...blocks, block]
}

// 思考增量并入末尾 reasoning 块；若上一步不是 reasoning 就新起一块。
// 这样 reasoning -> tool -> reasoning -> text 的真实顺序得以保留。
function appendReasoningBlock(blocks: any[], chunk: string): any[] {
  if (!chunk) return blocks
  const last = blocks[blocks.length - 1]
  if (last && last.type === 'reasoning') {
    return [...blocks.slice(0, -1), { ...last, text: (last.text || '') + chunk }]
  }
  return [...blocks, { type: 'reasoning', text: chunk }]
}


function mergeTeamEvent(prev: TeamSnapshot | null, event: TeamEvent): TeamSnapshot | null {
  // Team creation can fail (quota/DB) while the ordinary turn still runs.  Keep
  // the event even when no team snapshot exists, so the panel and the notice
  // bar can explain why no task board appeared.
  if (event.type === 'team_unavailable') {
    const base = prev || { team: null, tasks: [], events: [], messages: [] }
    const events = [...base.events.filter((e) => e.event_seq !== event.event_seq), event]
      .sort((a, b) => a.event_seq - b.event_seq)
    return { team: base.team, tasks: base.tasks, events, messages: base.messages }
  }
  // 新 Team 的 team_start 必须能替换旧代快照，否则同一会话下一轮生成期间
  // 会一直显示上一轮的旧任务板（generation/team id 都不同）。
  if (event.type === 'team_start' && (!prev?.team || prev.team.team_id !== event.team_id)) {
    const now = Date.now()
    return {
      team: {
        team_id: event.team_id, user_id: '', session_id: event.session_id || '',
        generation_id: event.generation_id || '', lead_agent_id: event.agent_id || 'root',
        name: typeof event.payload.name === 'string' ? event.payload.name : 'Agent Team',
        status: 'active', created_at: now, updated_at: now, closed_at: 0,
      },
      tasks: [], events: [event], messages: [],
    }
  }
  if (!prev || !prev.team) return prev
  if (event.team_id && prev.team.team_id !== event.team_id) return prev
  const events = prev.events.some((e) => e.event_seq === event.event_seq)
    ? prev.events
    : [...prev.events, event].sort((a, b) => a.event_seq - b.event_seq)
  let tasks = prev.tasks
  let messages = prev.messages
  let team = prev.team
  if (event.type === 'team_resumed') {
    // The backend snapshots the previous generation's open board.  Keep the
    // restored rows in the same snapshot so the panel updates on first paint.
    const restoredTasks = event.payload.tasks
    const restoredMessages = event.payload.messages
    if (Array.isArray(restoredTasks)) tasks = restoredTasks as TeamSnapshot['tasks']
    if (Array.isArray(restoredMessages)) messages = restoredMessages as TeamSnapshot['messages']
  } else if (event.type === 'task_created' || event.type === 'task_updated') {
    const task = event.payload.task as TeamSnapshot['tasks'][number] | undefined
    if (task?.task_id) {
      const exists = tasks.some((t) => t.task_id === task.task_id)
      tasks = exists ? tasks.map((t) => t.task_id === task.task_id ? task : t) : [...tasks, task]
    }
  } else if (event.type === 'team_message') {
    const message = event.payload.message as TeamSnapshot['messages'][number] | undefined
    if (message?.message_id && !messages.some((m) => m.message_id === message.message_id)) {
      messages = [...messages, message]
    }
  } else if (event.type === 'team_end') {
    const nextStatus = typeof event.payload.status === 'string'
      ? event.payload.status as typeof team.status
      : team.status
    team = { ...team, status: nextStatus }
  }
  return { team, tasks, events, messages }
}

export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [booting, setBooting] = useState(true)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const [sessions, setSessions] = useState<Session[]>([])
  const [allSessionsOpen, setAllSessionsOpen] = useState(false)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [streaming, setStreaming] = useState(false)
  const [sendPulse, setSendPulse] = useState(0)
  const [activeStreamMsgId, setActiveStreamMsgId] = useState<string | null>(null)
  const [autoCompacted, setAutoCompacted] = useState<number>(0)
  const [health, setHealth] = useState<
    { model: string; mcp_tools: number; servers: McpServerStatus[] } | null
  >(null)
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(null)
  const [models, setModels] = useState<LlmModelOption[]>([])
  // 没有会话时选择的模型：作为下一个新会话的初始 model_id
  const [pendingModelId, setPendingModelId] = useState<string>('')
  const [pendingEffort, setPendingEffort] = useState<string>('')
  const [effortEcho, setEffortEcho] = useState<{ effective: string; source: string; model: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [teamSnapshot, setTeamSnapshot] = useState<TeamSnapshot | null>(null)
  const [teamError, setTeamError] = useState<string | null>(null)
  const [teamOpen, setTeamOpen] = useState(false)
  const [teamGenerationId, setTeamGenerationId] = useState<string>('')
  const [notice, setNotice] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const genIdRef = useRef<string>('')
  // 服务端消息快照不含前端计时字段；按 message id 保存本轮起止时间，
  // 让其跨 message_start 改 ID、历史刷新、停止快照替换后仍能显示。
  const runTimesRef = useRef<Map<string, { started: number; finished?: number }>>(new Map())
  // 正在本端流式生成的会话 id；用于阻止历史加载覆盖乐观渲染的消息
  const streamSidRef = useRef<string | null>(null)

  // Restore the panel preference once the account is known, then persist it.
  // One effect keeps the restore from being overwritten by a premature write.
  const prevUserIdRef = useRef<string | null>(null)
  useEffect(() => {
    if (!user) {
      prevUserIdRef.current = null
      return
    }
    if (prevUserIdRef.current !== user.id) {
      prevUserIdRef.current = user.id
      setTeamOpen(readTeamOpen(user.id))
      return
    }
    try {
      localStorage.setItem(teamOpenKey(user.id), teamOpen ? '1' : '0')
    } catch {
      /* localStorage may be unavailable in hardened browsers. */
    }
  }, [user?.id, teamOpen])

  useEffect(() => {
    if (!notice) return
    const t = setTimeout(() => setNotice(null), 4000)
    return () => clearTimeout(t)
  }, [notice])

  useEffect(() => {
    if (!getToken()) {
      setBooting(false)
      return
    }
    api
      .me()
      .then((u) => setUser(u))
      .catch(() => setToken(''))
      .finally(() => setBooting(false))
  }, [])

  useEffect(() => {
    const handler = () => {
      setUser(null)
      setSessions([])
      setActiveId(null)
      setMessages([])
      setSettingsOpen(false)
    }
    window.addEventListener('auth:logout', handler)
    return () => window.removeEventListener('auth:logout', handler)
  }, [])

  const loadSessions = useCallback(async () => {
    try {
      const r = await api.listSessions()
      setSessions(r.sessions)
      return r.sessions
    } catch (e) {
      setError(String(e))
      return []
    }
  }, [])

  const loadModels = useCallback(async () => {
    try {
      const r = await api.listLlmModels()
      setModels(r.models)
    } catch {
      /* 忽略：无模型列表时下拉仅显示当前 */
    }
  }, [])

  const loadContextUsage = useCallback(async (id: string) => {
    try {
      setContextUsage(await api.getContextUsage(id))
    } catch {
      setContextUsage(null)
    }
  }, [])

  const loadMessages = useCallback(async (id: string) => {
    try {
      const d = await api.getSession(id)
      // 若该会话正在本端流式生成，勿用服务器历史覆盖乐观渲染的消息
      if (streamSidRef.current === id) return
      setMessages((prev) => {
        // 刚停止时服务端可能稍晚写入快照；若历史版本仍为空，而本地已有
        // 同一 message_id 的可见内容，就保留本地，避免“停止后内容消失”。
        return d.messages.map((serverMsg) => {
          const localMsg = prev.find((m) => m.id === serverMsg.id)
          if (!localMsg) return serverMsg
          const serverHasVisible =
            !!serverMsg.content || !!serverMsg.reasoning || (serverMsg.blocks || []).length > 0
          const localHasVisible =
            !!localMsg.content || !!localMsg.reasoning || (localMsg.blocks || []).length > 0
          const merged = !serverHasVisible && localHasVisible ? localMsg : serverMsg
          const timing = runTimesRef.current.get(merged.id)
          // 本地已结束而服务端快照仍是 streaming：保留结束态，避免尾部卡在 Working。
          const keepLocalEnd =
            localMsg.status !== 'streaming' &&
            merged.status === 'streaming' &&
            !!localMsg.run_finished_at
          return timing || keepLocalEnd
            ? {
                ...merged,
                run_started_at: timing?.started ?? localMsg.run_started_at ?? merged.run_started_at,
                run_finished_at:
                  timing?.finished ??
                  localMsg.run_finished_at ??
                  merged.run_finished_at,
                status: keepLocalEnd ? localMsg.status : merged.status,
              }
            : merged
        })
      })
    } catch (e) {
      setError(String(e))
    }
  }, [])

  const refreshHealth = useCallback(async () => {
    const h = await api.health().catch(() => null)
    const eff = await api.mcpEffective().catch(() => null)
    setHealth({
      model: h?.model || '',
      mcp_tools: eff ? eff.tools.length : h?.mcp_tools ?? 0,
      servers: eff?.servers || [],
    })
  }, [])

  useEffect(() => {
    if (!user) return
    refreshHealth()
    loadModels()
    loadSessions().then((list) => {
      if (list.length > 0) setActiveId(list[0].id)
    })
  }, [user, loadSessions, refreshHealth, loadModels])

  const loadTeam = useCallback(async (id: string) => {
    try {
      const snapshot = await api.getTeam(id)
      if (activeId === id || !activeId) setTeamSnapshot(snapshot)
    } catch {
      if (activeId === id || !activeId) setTeamSnapshot(null)
    }
  }, [activeId])

  useEffect(() => {
    if (!user || !activeId) {
      setTeamSnapshot(null)
      return
    }
    loadTeam(activeId)
  }, [user, activeId, loadTeam])

  useEffect(() => {
    if (!user) return
    if (activeId) {
      loadContextUsage(activeId)
      setAutoCompacted(0)
      // 新会话首条消息：正在本端生成，跳过历史加载以免清空乐观消息
      if (streamSidRef.current === activeId) return
      loadMessages(activeId)
    } else {
      setMessages([])
      setContextUsage(null)
    }
  }, [user, activeId, loadMessages, loadContextUsage])

  const doLogout = async () => {
    try {
      await api.logout()
    } catch {
      /* ignore */
    }
    setToken('')
    setUser(null)
    setSessions([])
    setActiveId(null)
    setMessages([])
    setSettingsOpen(false)
  }

  const selectSession = (id: string) => {
    if (streaming) return
    setActiveId(id)
  }

  const createSession = async () => {
    if (streaming) return
    try {
      const s = await api.createSession(undefined, pendingModelId || defaultModelId())
      await loadSessions()
      setActiveId(s.id)
      setMessages([])
    } catch (e) {
      setError(String(e))
    }
  }

  const deleteSession = async (id: string) => {
    if (streaming) return
    if (!confirm('确定删除该会话？')) return
    try {
      await api.deleteSession(id)
      const list = await loadSessions()
      if (activeId === id) setActiveId(list.length > 0 ? list[0].id : null)
    } catch (e) {
      setError(String(e))
    }
  }

  const renameSession = async (id: string, title: string) => {
    try {
      await api.renameSession(id, title)
      await loadSessions()
    } catch (e) {
      setError(String(e))
    }
  }

  const doCompact = async (instructions = ''): Promise<boolean> => {
    if (!activeId || streaming) return false
    setError(null)
    try {
      const r = await api.compact(activeId, { instructions: instructions || undefined })
      if (r.compacted) {
        setAutoCompacted((n) => n + 1)
        setNotice(r.message || '已压缩上下文')
        await loadMessages(activeId)
        await loadSessions()
        await loadContextUsage(activeId)
      } else {
        setNotice(r.message || '暂无可压缩的内容（消息较少）')
      }
      return r.compacted
    } catch (e) {
      setError(String(e))
      return false
    }
  }

  const stop = () => {
    if (!activeId) return
    const sid = activeId
    const genId = genIdRef.current
    const messageId = activeStreamMsgId || undefined

    // Stop is a local-first action. The user must regain the composer
    // immediately, even if the backend is stuck in an LLM/tool await. Mark the
    // visible assistant bubble terminal, abort our fetch, then ask the server
    // to cancel in the background. The backend stop endpoint uses task.cancel()
    // and bounded waits, so this parallel request normally resolves quickly.
    const finishedAt = Date.now()
    setMessages((prev) => prev.map((m) => {
      if (m.id !== messageId && m.status !== 'streaming') return m
      const timing = runTimesRef.current.get(m.id)
      if (timing) runTimesRef.current.set(m.id, { ...timing, finished: finishedAt })
      return { ...m, status: 'stopped', run_finished_at: m.run_finished_at || finishedAt }
    }))
    setStreaming(false)
    setActiveStreamMsgId(null)
    abortRef.current?.abort()

    api.stop(sid, genId, messageId).then((r) => {
      if (r.message) {
        const stopped = { ...r.message, status: 'stopped' as const }
        setMessages((prev) => prev.map((m) => (m.id === stopped.id ? { ...stopped } : m)))
      }
    }).catch(() => {
      /* Local stop already succeeded from the user's perspective. */
    }).finally(() => {
      // Pull the authoritative snapshot after the server has converged the
      // lease/team facts. This also repairs any hard-cutoff fallback.
      setTimeout(() => { loadMessages(sid); loadTeam(sid); loadContextUsage(sid) }, 250)
    })
  }

  const send = async (text: string, attachments: Attachment[] = []) => {
    if ((!text.trim() && attachments.length === 0) || streaming) return
    setError(null)
    setTeamSnapshot(null)
    setTeamError(null)

    const tempSid = activeId || 'pending'
    const genId = newGenId()
    setTeamGenerationId(genId)
    genIdRef.current = genId
    const ctrl = new AbortController()
    abortRef.current = ctrl

    const userMsg: Message = {
      id: 'local_user_' + Date.now(),
      session_id: tempSid,
      role: 'user',
      content: text,
      reasoning: '',
      blocks: [
        ...(text ? [{ type: 'text' as const, text }] : []),
        ...(attachments.length
          ? [{ type: 'attachments' as const, items: attachments }]
          : []),
      ],
      status: 'complete',
      created_at: Date.now(),
    }
    const runStartedAt = Date.now()
    const localAssistantId = 'local_assistant_' + Date.now()
    runTimesRef.current.set(localAssistantId, { started: runStartedAt })
    let assistantMsg: Message = {
      id: localAssistantId,
      session_id: tempSid,
      role: 'assistant',
      content: '',
      reasoning: '',
      blocks: [],
      status: 'streaming',
      created_at: runStartedAt,
      run_started_at: runStartedAt,
    }

    // 乐观更新：立即渲染用户消息与占位回复，避免等待网络时出现空白。
    // sendPulse 让 MessageList 把这次提交识别为“看最新消息”的意图，
    // 即使提交前用户正在上翻历史，也会立即滚到新消息。
    setSendPulse((n) => n + 1)
    setStreaming(true)
    setActiveStreamMsgId(localAssistantId)
    setMessages((prev) => [...prev, userMsg, assistantMsg])

    // 确保存在会话（若有则直接使用）
    let sid = activeId
    if (!sid) {
      try {
        const created = await api.createSession(undefined, pendingModelId || defaultModelId())
        sid = created.id
        streamSidRef.current = sid
        setActiveId(sid)
        loadSessions()
        const fixSid = (m: Message): Message =>
          m.id === userMsg.id || m.id === localAssistantId ? { ...m, session_id: sid! } : m
        setMessages((prev) => prev.map(fixSid))
      } catch (e) {
        setError(String(e))
        setStreaming(false)
        setActiveStreamMsgId(null)
        setMessages((prev) => prev.filter((m) => m.id !== userMsg.id && m.id !== localAssistantId))
        return
      }
    } else {
      streamSidRef.current = sid
    }

    // 压缩会向历史插入 system 摘要消息；本轮完成后重新加载会移除它们，
    // 因此从历史里挑出已有的 summary 块，附加到本次 assistant 消息上渲染，避免“闪现后消失”。
    const summaryBlocks: any[] = messages
      .flatMap((x) => x.blocks || [])
      .filter((b: any) => b && b.type === 'summary')
    const withSummaries = (m: Message): Message =>
      summaryBlocks.length ? { ...m, blocks: [...summaryBlocks, ...(m.blocks || [])] } : m

    const patch = (m: Message) => {
      assistantMsg = m
      setMessages((prev) => prev.map((x) => (x.id === m.id ? { ...m } : x)))
    }

    let blocks: any[] = []
    const onEvent = (ev: StreamEvent) => {
      if (ev.type === 'message_start') {
        const timing = runTimesRef.current.get(assistantMsg.id)
        if (timing) {
          runTimesRef.current.delete(assistantMsg.id)
          runTimesRef.current.set(ev.message_id, timing)
        }
        const updated = { ...assistantMsg, id: ev.message_id, run_started_at: runStartedAt }
        setActiveStreamMsgId(ev.message_id)
        setMessages((prev) =>
          prev.map((x) => (x.id === localAssistantId || x.id === assistantMsg.id ? updated : x)),
        )
        assistantMsg = updated
      } else if (ev.type === 'text_delta') {
        blocks = appendTextBlock(blocks, ev.text)
        patch({ ...assistantMsg, content: assistantMsg.content + ev.text, blocks: [...blocks] })
      } else if (ev.type === 'thinking_delta') {
        blocks = appendReasoningBlock(blocks, ev.text)
        patch({ ...assistantMsg, reasoning: assistantMsg.reasoning + ev.text, blocks: [...blocks] })
      } else if (ev.type === 'tool_start') {
        blocks = appendBlock(blocks, { type: 'tool', id: ev.id, name: ev.name, input: ev.input })
        patch({ ...assistantMsg, blocks: [...blocks] })
      } else if (ev.type === 'tool_result') {
        blocks = blocks.map((b) =>
          b.type === 'tool' && b.id === ev.id ? { ...b, result: ev.content } : b,
        )
        patch({ ...assistantMsg, blocks: [...blocks] })
      } else if (ev.type === 'chart') {
        blocks = appendBlock(blocks, { type: 'chart', spec: ev.spec })
        patch({ ...assistantMsg, blocks: [...blocks] })
      } else if (ev.type === 'subagent_start') {
        blocks = mergeStart(blocks as any, {
          agent_id: ev.agent_id, name: ev.name, role: ev.role,
          task: ev.task, startedAt: Date.now(),
        }) as any
        patch({ ...assistantMsg, blocks: [...blocks] })
      } else if (ev.type === 'subagent_event') {
        const inner: any = ev.event
        blocks = blocks.map((b: any) => {
          if (b.type !== 'subagent' || b.agent_id !== ev.agent_id) return b
          const events = [...(b.events || [])]
          if (inner.type === 'text_delta') events.push({ kind: 'text', text: inner.text })
          else if (inner.type === 'thinking_delta') events.push({ kind: 'reasoning', text: inner.text })
          else if (inner.type === 'tool_start')
            events.push({ kind: 'tool_start', id: inner.id, name: inner.name, input: inner.input })
          else if (inner.type === 'tool_result')
            events.push({ kind: 'tool_result', id: inner.id, name: inner.name, result: inner.content, is_error: inner.is_error })
          return { ...b, events }
        })
        patch({ ...assistantMsg, blocks: [...blocks] })
      } else if (ev.type === 'subagent_status') {
        blocks = blocks.map((b: any) =>
          b.type === 'subagent' && b.agent_id === ev.agent_id
            ? { ...b, status: mergeStatus(b.status, ev.status) }
            : b,
        )
        patch({ ...assistantMsg, blocks: [...blocks] })
      } else if (ev.type === 'subagent_end') {
        blocks = blocks.map((b: any) =>
          b.type === 'subagent' && b.agent_id === ev.agent_id
            ? {
                ...b,
                status: mergeStatus(b.status, ev.status),
                endedAt: Date.now(),
                result: ev.result || b.result,
                turns: ev.turns ?? b.turns,
                error: ev.error || b.error,
              }
            : b,
        )
        patch({ ...assistantMsg, blocks: [...blocks] })
      } else if (ev.type === 'team_event') {
        setTeamGenerationId(genId)
        setTeamSnapshot((prev) => mergeTeamEvent(prev, ev.event))
        if (ev.event.type === 'team_unavailable') {
          const reason = ev.event.payload?.error
          setTeamError(typeof reason === 'string' && reason ? reason : '未知原因')
        } else if (ev.event.type === 'team_start') {
          setTeamError(null)
        }
      } else if (ev.type === 'reasoning_effort') {
        // 后端回报本轮实际发出去的等级：有些网关会静默忽略不认识的档位，
        // 用户需要看到「设了但可能没生效」，而不是以为自己选对了。
        setEffortEcho({ effective: ev.effective || '', source: ev.source || '', model: ev.model || '' })
      } else if (ev.type === 'compaction') {
        setAutoCompacted((n) => n + 1)
      } else if (ev.type === 'done') {
        const status = ev.status === 'stopped' ? 'stopped' : ev.status === 'error' ? 'error' : 'complete'
        const finishedAt = Date.now()
        runTimesRef.current.set(assistantMsg.id, { started: runStartedAt, finished: finishedAt })
        patch(withSummaries({ ...assistantMsg, status, run_finished_at: finishedAt }))
      } else if (ev.type === 'error') {
        const finishedAt = Date.now()
        runTimesRef.current.set(assistantMsg.id, { started: runStartedAt, finished: finishedAt })
        setError(ev.error)
        patch({ ...assistantMsg, status: 'error', run_finished_at: finishedAt })
      }
    }

    try {
      await streamMessage(sid, text, genId, onEvent, ctrl.signal, attachments,
                          activeSession?.reasoning_effort || pendingEffort || undefined)
    } catch (e) {
      if (!ctrl.signal.aborted) {
        const msg = String(e)
        // A stale server lease is usually auto-converged on the next send, but
        // a real concurrent generation still needs an explicit user action.
        // Surface an actionable message instead of a cryptic 409.
        if (msg.includes('generation_active') || msg.includes('当前仍在生成')) {
          setError('当前会话仍在生成。请点击「停止」，或刷新页面后再发送。')
        } else {
          setError(msg)
        }
      }
    } finally {
      // 停止/网络中断时服务端不一定来得及发 done；给本地气泡补上结束时间，
      // 让尾部状态从“执行中”可靠切换为“已停止/可继续输入”。
      if (assistantMsg.status === 'streaming') {
        const finishedAt = Date.now()
        runTimesRef.current.set(assistantMsg.id, { started: runStartedAt, finished: finishedAt })
        assistantMsg = {
          ...assistantMsg,
          status: ctrl.signal.aborted ? 'stopped' : 'error',
          run_finished_at: finishedAt,
        }
        setMessages((prev) => prev.map((m) => (m.id === assistantMsg.id ? assistantMsg : m)))
      }
      // Local-first stop unlocks the composer before this old stream has fully
      // unwound. If the user already sent the next message, genIdRef points to
      // a newer generation; do not let this stale finally block clear its refs
      // or flip streaming back to false mid-run.
      if (genIdRef.current === genId) {
        setStreaming(false)
        setActiveStreamMsgId(null)
        if (streamSidRef.current === sid) streamSidRef.current = null
        if (abortRef.current === ctrl) abortRef.current = null
      }
      // 停止是“用户主动结束”而不是失败：本地已经显示的文本/工具/图片必须保留。
      // 等 stop 端点把快照写入 DB 后，再做一次合并式刷新。
      if (ctrl.signal.aborted) {
        setTimeout(() => loadMessages(sid!), 350)
      } else {
        setTimeout(() => loadMessages(sid!), 150)
      }
      loadSessions()
      if (sid) {
        setTimeout(() => loadTeam(sid), 180)
        setTimeout(() => loadContextUsage(sid), 180)
      }
    }
  }

  const defaultModelId = () => {
    const d = models.find((m) => m.is_default)
    return d ? d.id : models[0]?.id || ''
  }

  const changeModel = async (modelId: string) => {
    if (streaming) return
    if (!activeId) {
      // 还没有会话：只记住选择，新建会话时带上，不必先发一条消息。
      setPendingModelId(modelId)
      setError(null)
      return
    }
    try {
      await api.setSessionModel(activeId, modelId)
      setPendingModelId(modelId)
      await loadSessions()
      await loadContextUsage(activeId)
      setError(null)
    } catch (e) {
      setError(String(e))
    }
  }

  if (booting) {
    return <div className="booting">加载中…</div>
  }

  if (!user) {
    return <LoginPage onAuthed={(u) => setUser(u)} />
  }

  const activeSession = sessions.find((s) => s.id === activeId) || null
  // 当前选择器应显示的值：有会话则用会话的 model_id，否则用「新会话默认模型」。
  const currentModelId = activeSession
    ? (activeSession.model_id || '')
    : pendingModelId || defaultModelId()

  const activeModel = (() => {
    if (!activeSession) {
      // 没有会话时，显示当前选中（或默认）的模型，避免侧栏误显示「未配置模型」。
      return (
        models.find((x) => x.id === currentModelId) ||
        models.find((x) => x.is_default) ||
        models[0] ||
        null
      )
    }
    return models.find((x) => x.id === (activeSession.model_id || '')) || null
  })()
  const activeModelLabel = activeModel ? activeModel.model : null

  // 思考等级：会话级覆盖，空=沿用模型配置。放在模型选择器后面。
  const currentEffort = activeSession ? (activeSession.reasoning_effort || '') : ''

  const changeEffort = async (effort: string) => {
    if (streaming) return
    if (!activeId) {
      // 还没有会话：只记住，创建会话后的第一条消息会带上（后端按消息级覆盖处理）。
      setPendingEffort(effort)
      return
    }
    try {
      await api.setSessionReasoningEffort(activeId, effort)
      await loadSessions()
      setError(null)
    } catch (e) {
      setError(String(e))
    }
  }

  // 选了档位、但后端回报的实际生效值与选择不一致 → 说明上游没认这个档位。
  // 这类网关是静默忽略的，不提示的话用户会一直以为生效了。
  const chosenEffort = currentEffort || pendingEffort
  const effortMismatch = Boolean(
    effortEcho && effortEcho.effective && chosenEffort && effortEcho.effective !== chosenEffort,
  )

  const effortSelector = (
    <span className="effort-wrap">
      {effortMismatch && (
        <span
          className="effort-warn"
          title={`本轮实际发出的等级是 ${effortEcho!.effective}，与你选择的 ${chosenEffort} 不一致；该模型可能不支持这一档`}
        >
          ⚠ 未生效
        </span>
      )}
      <ReasoningMenu value={chosenEffort} disabled={streaming} onChange={changeEffort} />
    </span>
  )

  // 模型选择器：底部说明行右侧；用自定义下拉，避免把「（默认）」塞进值里被截断。
  const modelSelector = (
    <ModelMenu
      models={models}
      currentId={currentModelId}
      disabled={streaming}
      onChange={changeModel}
    />
  )

  return (
    <div className="app">
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        health={health}
        user={user}
        activeModel={activeModelLabel}
        onSelect={selectSession}
        onCreate={createSession}
        onDelete={deleteSession}
        onRename={renameSession}
        onOpenSettings={() => setSettingsOpen(true)}
        onLogout={doLogout}
        onOpenAllSessions={() => setAllSessionsOpen(true)}
      />
      <div className="main">
        <div className="main-header">
          <button type="button" className="chat-title-btn" title="会话标题">
            <span className="t">{activeSession?.title || 'Hlaudz'}</span>
            <span className="caret" aria-hidden="true">▾</span>
          </button>
          <div className="header-actions">
            <button
              type="button"
              className="ghost-btn"
              onClick={() => setWorkspaceOpen(true)}
              title="浏览、下载 workspace 中的任意文件"
            >
              文件
            </button>
            <button type="button" className="ghost-btn" disabled title="分享（未实现）">
              分享
            </button>
          </div>
        </div>
        {teamError && (
          <div className="notice-bar team-warn">
            Team 本轮不可用：{teamError}
            <button className="ghost-btn" style={{ marginLeft: 10 }} onClick={() => setTeamError(null)}>
              关闭
            </button>
          </div>
        )}
        {notice && (
          <div className="notice-bar">
            {notice}
            <button className="ghost-btn" style={{ marginLeft: 10 }} onClick={() => setNotice(null)}>
              关闭
            </button>
          </div>
        )}
        {error && (
          <div style={{ padding: '8px 18px', background: '#fef2f2', color: '#dc2626', borderBottom: '1px solid #f5c2c0', fontSize: 13 }}>
            {error}
            <button className="ghost-btn" style={{ marginLeft: 10 }} onClick={() => setError(null)}>
              忽略
            </button>
          </div>
        )}
        <MessageList
          messages={messages}
          streaming={streaming}
          autoCompacted={autoCompacted}
          activeStreamMsgId={activeStreamMsgId}
          sessionKey={activeId}
          sendPulse={sendPulse}
          onOpenWorkspace={() => setWorkspaceOpen(true)}
        />
        <Composer
          streaming={streaming}
          onSend={send}
          onStop={stop}
          onCompact={doCompact}
          contextUsage={contextUsage}
          modelName={activeModel?.model || ''}
          reasoningEffort={activeModel?.reasoning_effort || ''}
          modelSelector={modelSelector}
          effortSelector={effortSelector}
        />
      </div>
      <SessionBrowser
        open={allSessionsOpen}
        activeId={activeId}
        initial={sessions}
        onClose={() => setAllSessionsOpen(false)}
        onSelect={(id) => { selectSession(id); setAllSessionsOpen(false) }}
        onDelete={deleteSession}
      />
      <TeamPanel
        snapshot={teamSnapshot}
        open={teamOpen}
        streaming={streaming}
        onToggle={() => setTeamOpen((v) => !v)}
        onStop={stop}
      />
      {workspaceOpen && (
        <WorkspacePanel open={workspaceOpen} onClose={() => setWorkspaceOpen(false)} />
      )}
      {settingsOpen && (
        <SettingsModal
          onLlmChanged={loadModels}
          onClose={() => {
            setSettingsOpen(false)
            loadModels()
            refreshHealth()
          }}
        />
      )}
    </div>
  )
}
