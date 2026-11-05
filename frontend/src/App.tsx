import { useCallback, useEffect, useRef, useState } from 'react'
import { api, getToken, setToken, streamMessage } from './api'
import type { LlmModelOption, Message, Session, StreamEvent, User } from './types'
import Sidebar from './components/Sidebar'
import MessageList from './components/MessageList'
import Composer from './components/Composer'
import LoginPage from './components/LoginPage'
import SettingsModal from './components/SettingsModal'

function newGenId() {
  return 'g_' + Math.random().toString(16).slice(2, 10)
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

export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [booting, setBooting] = useState(true)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [streaming, setStreaming] = useState(false)
  const [activeStreamMsgId, setActiveStreamMsgId] = useState<string | null>(null)
  const [autoCompacted, setAutoCompacted] = useState<number>(0)
  const [health, setHealth] = useState<{ model: string; mcp_tools: number } | null>(null)
  const [models, setModels] = useState<LlmModelOption[]>([])
  // 没有会话时选择的模型：作为下一个新会话的初始 model_id
  const [pendingModelId, setPendingModelId] = useState<string>('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const genIdRef = useRef<string>('')
  // 正在本端流式生成的会话 id；用于阻止历史加载覆盖乐观渲染的消息
  const streamSidRef = useRef<string | null>(null)

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
          return !serverHasVisible && localHasVisible ? localMsg : serverMsg
        })
      })
    } catch (e) {
      setError(String(e))
    }
  }, [])

  const refreshHealth = useCallback(async () => {
    const h = await api.health().catch(() => null)
    const eff = await api.mcpEffective().catch(() => null)
    setHealth({ model: h?.model || '', mcp_tools: eff ? eff.tools.length : h?.mcp_tools ?? 0 })
  }, [])

  useEffect(() => {
    if (!user) return
    refreshHealth()
    loadModels()
    loadSessions().then((list) => {
      if (list.length > 0) setActiveId(list[0].id)
    })
  }, [user, loadSessions, refreshHealth, loadModels])

  useEffect(() => {
    if (!user) return
    if (activeId) {
      setAutoCompacted(0)
      // 新会话首条消息：正在本端生成，跳过历史加载以免清空乐观消息
      if (streamSidRef.current === activeId) return
      loadMessages(activeId)
    } else {
      setMessages([])
    }
  }, [user, activeId, loadMessages])

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
      } else {
        setNotice(r.message || '暂无可压缩的内容（消息较少）')
      }
      return r.compacted
    } catch (e) {
      setError(String(e))
      return false
    }
  }

  const stop = async () => {
    if (!activeId) return
    const messageId = activeStreamMsgId || undefined
    // 先把“用户已看到的本地消息”保留下来。stop 接口返回服务端已落库快照；
    // 若服务端还来不及写，本地 snapshot 仍会显示，待重新加载时再与服务端合并。
    let stoppedMsg: Message | null = null
    try {
      const r = await api.stop(activeId, genIdRef.current, messageId || undefined)
      if (r.message) stoppedMsg = r.message
    } catch {
      /* ignore */
    }
    if (stoppedMsg) {
      setMessages((prev) => prev.map((m) => (m.id === stoppedMsg!.id ? { ...stoppedMsg!, status: 'stopped' } : m)))
    } else {
      setMessages((prev) => prev.map((m) => (m.id === messageId ? { ...m, status: 'stopped' } : m)))
    }
    abortRef.current?.abort()
  }

  const send = async (text: string) => {
    if (!text.trim() || streaming) return
    setError(null)

    const tempSid = activeId || 'pending'
    const genId = newGenId()
    genIdRef.current = genId
    const ctrl = new AbortController()
    abortRef.current = ctrl

    const userMsg: Message = {
      id: 'local_user_' + Date.now(),
      session_id: tempSid,
      role: 'user',
      content: text,
      reasoning: '',
      blocks: [{ type: 'text', text }],
      status: 'complete',
      created_at: Date.now(),
    }
    let assistantMsg: Message = {
      id: 'local_assistant_' + Date.now(),
      session_id: tempSid,
      role: 'assistant',
      content: '',
      reasoning: '',
      blocks: [],
      status: 'streaming',
      created_at: Date.now(),
    }
    const localAssistantId = assistantMsg.id

    // 乐观更新：立即渲染用户消息与占位回复，避免等待网络时出现空白
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
        const updated = { ...assistantMsg, id: ev.message_id }
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
        blocks = appendBlock(blocks, {
          type: 'subagent', agent_id: ev.agent_id, name: ev.name, role: ev.role,
          status: 'running', task: ev.task, events: [], result: '', turns: 0,
        })
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
      } else if (ev.type === 'subagent_end') {
        blocks = blocks.map((b: any) =>
          b.type === 'subagent' && b.agent_id === ev.agent_id
            ? { ...b, status: ev.status, result: ev.result || b.result, turns: ev.turns ?? b.turns, error: ev.error || b.error }
            : b,
        )
        patch({ ...assistantMsg, blocks: [...blocks] })
      } else if (ev.type === 'compaction') {
        setAutoCompacted((n) => n + 1)
      } else if (ev.type === 'done') {
        const status = ev.status === 'stopped' ? 'stopped' : ev.status === 'error' ? 'error' : 'complete'
        patch(withSummaries({ ...assistantMsg, status }))
      } else if (ev.type === 'error') {
        setError(ev.error)
        patch({ ...assistantMsg, status: 'error' })
      }
    }

    try {
      await streamMessage(sid, text, genId, onEvent, ctrl.signal)
    } catch (e) {
      if (!ctrl.signal.aborted) setError(String(e))
    } finally {
      setStreaming(false)
      setActiveStreamMsgId(null)
      streamSidRef.current = null
      abortRef.current = null
      // 停止是“用户主动结束”而不是失败：本地已经显示的文本/工具/图片必须保留。
      // 等 stop 端点把快照写入 DB 后，再做一次合并式刷新。
      if (ctrl.signal.aborted) {
        setTimeout(() => loadMessages(sid!), 350)
      } else {
        setTimeout(() => loadMessages(sid!), 150)
      }
      loadSessions()
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

  const activeModelLabel = (() => {
    if (!activeSession) {
      // 没有会话时，显示当前选中（或默认）的模型，避免侧栏误显示「未配置模型」。
      const d =
        models.find((x) => x.id === currentModelId) ||
        models.find((x) => x.is_default) ||
        models[0]
      return d ? d.model : null
    }
    const m = models.find((x) => x.id === (activeSession.model_id || ''))
    return m ? m.model : null
  })()

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
      />
      <div className="main">
        <div className="main-header">
          <div className="title">{activeSession?.title || 'Web Agent'}</div>
          {/* 模型选择器始终可见：没有会话时也能选，选中的值作为「新会话的默认模型」。
              之前只在 activeSession 存在时渲染，导致刚配置完模型、还没发消息时完全看不到下拉框。 */}
          <label className="model-picker" title={activeSession ? '切换本会话使用的模型（会话级）' : '新会话将使用该模型'}>
            <span className="model-picker-label">模型</span>
            <select
              value={currentModelId}
              disabled={streaming || models.length === 0}
              onChange={(e) => changeModel(e.target.value)}
            >
              {models.length === 0 && (
                <option value="">
                  {activeSession?.model_id ? '（该配置不可用）' : '（尚未配置模型）'}
                </option>
              )}
              {/* 会话记录的模型已不可用（被删除/停用）时，避免下拉框显示为空白 */}
              {models.length > 0 && currentModelId !== '' &&
                !models.some((m) => m.id === currentModelId) && (
                  <option value={currentModelId}>（该配置不可用）</option>
                )}
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label} · {m.model}
                  {m.is_default ? '（默认）' : ''}
                </option>
              ))}
            </select>
          </label>
          {activeSession && (
            <span className="badge">
              工具 {health?.mcp_tools ?? 0} · 压缩 {activeSession.compaction?.compacted ? '已启用' : '未触发'}
            </span>
          )}
          <button className="ghost-btn" onClick={() => doCompact()} disabled={!activeId || streaming}>
            压缩上下文
          </button>
        </div>
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
        />
        <Composer streaming={streaming} onSend={send} onStop={stop} onCompact={doCompact} />
      </div>
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
