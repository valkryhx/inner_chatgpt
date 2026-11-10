import { useEffect, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent } from 'react'
import { api } from '../api'
import type { LLMConfig, MCPServer, McpServerStatus, Skill } from '../types'
import A2aSection from './A2aSection'

interface Props {
  onClose: () => void
  onLlmChanged?: () => void
}

const MODEL_PRESETS = [
  { value: 'deepseek-v4.1-flash', label: 'DeepSeek V4.1 Flash' },
  { value: 'deepseek-flash', label: 'DeepSeek Flash' },
  { value: 'deepseek-chat', label: 'DeepSeek Chat' },
  { value: 'deepseek-reasoner', label: 'DeepSeek Reasoner' },
  { value: 'glm-5.3-flash', label: 'GLM 5.3 Flash' },
  { value: 'glm-4.7', label: 'GLM 4.7' },
  { value: 'qwen3-max', label: 'Qwen3 Max' },
  { value: 'kimi-k2.5', label: 'Kimi K2.5' },
  { value: 'gpt-5.2', label: 'GPT-5.2' },
  { value: 'gpt-5-mini', label: 'GPT-5 Mini' },
] as const

const MODEL_CUSTOM = '__custom__'

const emptyLlm = {
  label: '',
  wire_api: 'openai_responses',
  base_url: 'https://api.deepseek.com',
  api_key: '',
  model: 'deepseek-v4.1-flash',
  context_window: 128000,
  max_tokens: 8192,
  auto_compact_tokens: null as number | null,
  hard_limit_tokens: null as number | null,
  reasoning_effort: '',
  is_default: true,
}


export default function SettingsModal({ onClose, onLlmChanged }: Props) {
  const [tab, setTab] = useState<'llm' | 'mcp' | 'skills' | 'a2a'>('llm')
  // 只在“按下与松开都发生在遮罩本身”时关闭。
  // 仅监听 click 会出现：在 textarea 内拖选文本/拖动滚动条，按下在弹窗内，
  // 松开落到遮罩上，浏览器把 click 派发给共同祖先 modal-mask，导致输入中途弹窗被关闭。
  const maskPressedRef = useRef(false)

  const onMaskMouseDown = (e: ReactMouseEvent<HTMLDivElement>) => {
    maskPressedRef.current = e.target === e.currentTarget
  }

  const onMaskClick = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (maskPressedRef.current && e.target === e.currentTarget) onClose()
    maskPressedRef.current = false
  }

  useEffect(() => {
    // 拖选文本时切窗/松手到窗口外，浏览器可能把后续 click 合成为遮罩点击。
    // 失焦即取消本次资格，恢复焦点后仍必须重新从遮罩按下。
    const resetMaskPress = () => { maskPressedRef.current = false }
    window.addEventListener('blur', resetMaskPress)
    return () => window.removeEventListener('blur', resetMaskPress)
  }, [])

  return (
    <div className="modal-mask" onMouseDown={onMaskMouseDown} onClick={onMaskClick}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>我的设置</h2>
          <button className="ghost-btn" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="modal-tabs">
          <button className={tab === 'llm' ? 'active' : ''} onClick={() => setTab('llm')}>
            我的 APIKey
          </button>
          <button className={tab === 'mcp' ? 'active' : ''} onClick={() => setTab('mcp')}>
            我的 MCP
          </button>
          <button className={tab === 'skills' ? 'active' : ''} onClick={() => setTab('skills')}>
            我的 Skills
          </button>
          <button className={tab === 'a2a' ? 'active' : ''} onClick={() => setTab('a2a')}>
            A2A 接入
          </button>
        </div>
        <div className="modal-body">
          {tab === 'llm' ? (
            <LlmSection onChanged={onLlmChanged} />
          ) : tab === 'mcp' ? (
            <McpSection />
          ) : tab === 'a2a' ? (
            <A2aSection />
          ) : (
            <SkillsSection />
          )}
        </div>
      </div>
    </div>
  )
}

function LlmSection({ onChanged }: { onChanged?: () => void }) {
  const [list, setList] = useState<LLMConfig[]>([])
  const [form, setForm] = useState<{ [k: string]: any }>({ ...emptyLlm })
  const [error, setError] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [customModel, setCustomModel] = useState(false)

  const load = () => api.listLlmConfigs().then(setList).catch((e) => setError(String(e)))
  useEffect(() => {
    load()
  }, [])

  const reset = () => {
    setForm({ ...emptyLlm })
    setEditingId(null)
    setCustomModel(false)
  }

  const save = async () => {
    setError(null)
    try {
      const body: any = { ...form }
      if (!body.api_key) delete body.api_key
      if (editingId) await api.updateLlmConfig(editingId, body)
      else await api.createLlmConfig(body)
      reset()
      await load()
      onChanged?.()
    } catch (e: any) {
      setError(e?.message || String(e))
    }
  }

  const edit = (c: LLMConfig) => {
    setEditingId(c.id)
    setCustomModel(false)
    setForm({
      label: c.label,
      wire_api: c.wire_api,
      base_url: c.base_url,
      api_key: '',
      model: c.model,
      context_window: c.context_window,
      max_tokens: c.max_tokens,
      auto_compact_tokens: c.auto_compact_tokens ?? null,
      hard_limit_tokens: c.hard_limit_tokens ?? null,
      reasoning_effort: c.reasoning_effort,
      is_default: c.is_default,
    })
  }

  const del = async (id: string) => {
    if (!confirm('删除该配置？')) return
    await api.deleteLlmConfig(id)
    await load()
    onChanged?.()
  }

  const setF = (k: string, v: any) => setForm((f) => ({ ...f, [k]: v }))

  // 既有配置里可能包含任意网关模型 ID；它们也必须出现在下拉中，
  // 否则编辑配置时会被误判成“自定义”而丢失当前值。
  const modelOptions = (() => {
    const byValue = new Map<string, string>()
    for (const p of MODEL_PRESETS) byValue.set(p.value, p.label)
    for (const c of list) {
      if (c.model && !byValue.has(c.model)) byValue.set(c.model, c.model)
    }
    if (form.model && !byValue.has(form.model)) byValue.set(form.model, form.model)
    return Array.from(byValue, ([value, label]) => ({ value, label }))
  })()
  const knownModel = modelOptions.some((m) => m.value === form.model)
  const modelSelectValue = customModel || !knownModel ? MODEL_CUSTOM : form.model
  const selectModel = (value: string) => {
    if (value === MODEL_CUSTOM) {
      setCustomModel(true)
      if (knownModel) setF('model', '')
      return
    }
    setCustomModel(false)
    setF('model', value)
  }

  return (
    <div className="settings-section">
      <p className="hint">配置你自己的模型端点。留空 api_key 表示编辑时不修改；默认配置将用于你的会话，未配置则回退系统默认模型。</p>
      <div className="cfg-list">
        {list.length === 0 && <div className="hint">尚未配置（将回退到系统默认模型）</div>}
        {list.map((c) => (
          <div className="cfg-item" key={c.id}>
            <div>
              <b>{c.label || c.model}</b> {c.is_default && <span className="tag">默认</span>}
              <div className="cfg-meta">
                {c.wire_api} · {c.model} · key {c.has_api_key ? c.api_key_masked : '未设置'}
              </div>
            </div>
            <div className="cfg-actions">
              <button className="ghost-btn" onClick={() => edit(c)}>
                编辑
              </button>
              <button className="ghost-btn danger" onClick={() => del(c.id)}>
                删除
              </button>
            </div>
          </div>
        ))}
      </div>
      <div className="form-grid">
        <label>
          <span>标签</span>
          <input value={form.label} onChange={(e) => setF('label', e.target.value)} placeholder="如 我的DeepSeek" />
        </label>
        <label>
          <span>协议</span>
          <select value={form.wire_api} onChange={(e) => setF('wire_api', e.target.value)}>
            <option value="openai_responses">openai_responses</option>
            <option value="openai_chat">openai_chat</option>
          </select>
        </label>
        <label className="full">
          <span>Base URL</span>
          <input value={form.base_url} onChange={(e) => setF('base_url', e.target.value)} />
        </label>
        <label className="full">
          <span>API Key（{editingId ? '留空不改' : '必填'}）</span>
          <input
            type="password"
            value={form.api_key}
            onChange={(e) => setF('api_key', e.target.value)}
            placeholder="sk-..."
          />
        </label>
        <label>
          <span>模型名</span>
          <select value={modelSelectValue} onChange={(e) => selectModel(e.target.value)}>
            {modelOptions.map((m) => (
              <option key={m.value} value={m.value}>{m.label}（{m.value}）</option>
            ))}
            <option value={MODEL_CUSTOM}>自定义模型 ID…</option>
          </select>
          {modelSelectValue === MODEL_CUSTOM && (
            <input
              value={form.model}
              onChange={(e) => {
                setCustomModel(true)
                setF('model', e.target.value)
              }}
              placeholder="输入模型 ID，如 my-gateway-model"
              autoFocus
            />
          )}
          <span className="field-help">可从常见模型中选择，也可选“自定义模型 ID”填写任意网关模型名。</span>
        </label>
        <label>
          <span>上下文窗口</span>
          <input
            type="number"
            value={form.context_window}
            onChange={(e) => setF('context_window', parseInt(e.target.value || '0'))}
          />
        </label>
        <label>
          <span>max_tokens</span>
          <input type="number" value={form.max_tokens} onChange={(e) => setF('max_tokens', parseInt(e.target.value || '0'))} />
        </label>
        <label>
          <span>自动压缩阈值 tokens（空=按窗口×0.90）</span>
          <input
            type="number"
            value={form.auto_compact_tokens ?? ''}
            placeholder={'派生 ' + Math.round((form.context_window || 0) * 0.9)}
            onChange={(e) => setF('auto_compact_tokens', e.target.value === '' ? null : parseInt(e.target.value || '0'))}
          />
        </label>
        <label>
          <span>硬线阈值 tokens（空=按窗口×0.95）</span>
          <input
            type="number"
            value={form.hard_limit_tokens ?? ''}
            placeholder={'派生 ' + Math.round((form.context_window || 0) * 0.95)}
            onChange={(e) => setF('hard_limit_tokens', e.target.value === '' ? null : parseInt(e.target.value || '0'))}
          />
        </label>
        <label>
          <span>reasoning_effort</span>
          <input value={form.reasoning_effort} onChange={(e) => setF('reasoning_effort', e.target.value)} placeholder="可空" />
        </label>
        <label className="checkbox">
          <input type="checkbox" checked={form.is_default} onChange={(e) => setF('is_default', e.target.checked)} />
          <span>设为默认</span>
        </label>
      </div>
      {error && <div className="auth-error">{error}</div>}
      <div className="form-actions">
        <button className="new-btn small" onClick={save}>
          {editingId ? '保存修改' : '新增配置'}
        </button>
        {editingId && (
          <button className="ghost-btn" onClick={reset}>
            取消
          </button>
        )}
      </div>
    </div>
  )
}

function McpSection() {
  const [list, setList] = useState<MCPServer[]>([])
  const [error, setError] = useState<string | null>(null)
  const [effective, setEffective] = useState<string[]>([])
  const [servers, setServers] = useState<McpServerStatus[]>([])
  const [probing, setProbing] = useState(false)
  const [jsonText, setJsonText] = useState('')
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // 用户一旦编辑，就不再被异步的“导出回显”覆盖（否则会清空刚粘贴的内容）
  const dirtyRef = useRef(false)

  const load = (syncText = false) => {
    api.listMcpServers().then(setList).catch((e) => setError(String(e)))
    api
      .mcpEffective()
      .then((r) => {
        setEffective(r.tools)
        setServers(r.servers || [])
      })
      .catch(() => {})
    api
      .mcpExport()
      .then((r) => {
        if (syncText || !dirtyRef.current) {
          setJsonText(r.json)
          dirtyRef.current = false
        }
      })
      .catch(() => {})
  }
  useEffect(() => {
    load(true)
  }, [])

  const formatJson = () => {
    try {
      setJsonText(JSON.stringify(JSON.parse(jsonText), null, 2))
      setError(null)
    } catch (e: any) {
      setError('JSON 格式错误：' + (e?.message || String(e)))
    }
  }

  const saveJson = async () => {
    setError(null)
    setNotice(null)
    setBusy(true)
    try {
      const r = await api.mcpImport(jsonText)
      const counts: string[] = []
      if (r.added.length) counts.push(`新增 ${r.added.length}`)
      if (r.updated.length) counts.push(`更新 ${r.updated.length}`)
      if (r.skipped.length) counts.push(`跳过 ${r.skipped.length}`)
      setNotice(
        (r.message || '导入完成') + (counts.length ? `（${counts.join('，')}）` : ''),
      )
      // Per-server failures are returned in OK responses; a bare "导入完成"
      // hides them and the user cannot tell which entry was rejected.
      if (r.errors?.length) {
        setError('以下条目未导入：' + r.errors.join('；'))
      }
      dirtyRef.current = false
      await load(true)
    } catch (e: any) {
      setError('导入失败：' + (e?.message || String(e)))
    } finally {
      setBusy(false)
    }
  }

  // runtime 没有 TTL：MCP 断开后缓存里的「已连接」会一直留着。
  // 必须提供一个绕开缓存、真实重连的入口，用户才能确认当前到底通不通。
  const probe = async () => {
    setProbing(true)
    setError(null)
    setNotice(null)
    try {
      const r = await api.mcpProbe()
      setServers(r.servers || [])
      setEffective(r.tools || [])
      if (!r.ok && r.error) setError('检测失败：' + r.error)
      else {
        const bad = (r.servers || []).filter((s) => !s.ok)
        setNotice(bad.length ? `检测完成：${bad.length} 个 server 连接失败` : '检测完成：全部已连接')
      }
    } catch (e: any) {
      setError('检测失败：' + (e?.message || String(e)))
    } finally {
      setProbing(false)
    }
  }

  const del = async (id: string) => {
    if (!confirm('删除该 MCP 服务器？')) return
    await api.deleteMcpServer(id)
    await load()
  }

  const EXAMPLE = `{
  "mcpServers": {
    "fetch": { "type": "stdio", "command": "npx", "args": ["-y", "mcp-fetch-server"] },
    "tavily": { "type": "http", "url": "https://mcp.tavily.com/mcp/?tavilyApiKey=YOUR_KEY" }
  }
}`

  return (
    <div className="settings-section">
      <p className="hint">
        按业界标准 <code>mcp.json</code>（<code>mcpServers</code>）直接粘贴配置即可。
        保存时按 <b>name</b> 增量合并：同名覆盖、新名新增，<b>不会清空</b>你已有的其它 MCP。
        （无配置时默认使用内置 demo 工具集）
      </p>

      <div className="mcp-summary">
        <div className="mcp-summary-row">
          <span className={`mcp-dot ${servers.length && servers.every((s) => s.ok) ? 'ok' : 'warn'}`} />
          <b>
            {servers.length === 0
              ? '未检测'
              : servers.every((s) => s.ok)
              ? `全部 ${servers.length} 个 server 已连接`
              : `${servers.filter((s) => s.ok).length}/${servers.length} 个 server 已连接`}
          </b>
          <span className="mcp-summary-tools">共 {effective.length} 个可用工具</span>
          <button className="ghost-btn small" onClick={probe} disabled={probing}>
            {probing ? '检测中…' : '重新检测连接'}
          </button>
        </div>
        {servers.filter((s) => !s.ok).length > 0 && (
          <div className="mcp-summary-bad">
            连接失败：
            {servers
              .filter((s) => !s.ok)
              .map((s) => `${s.name}（${s.error || '未知原因'}）`)
              .join('、')}
          </div>
        )}
      </div>

      <div className="cfg-list">
        {list.length === 0 && <div className="hint">尚未配置（使用内置 demo 工具）</div>}
        {list.map((m) => (
          <div className="cfg-item" key={m.id}>
            <div>
              <b>{m.name || m.transport}</b> <span className="tag">{m.transport}</span>
              <div className="cfg-meta">
                {m.transport === 'builtin_demo'
                  ? '内置 demo 工具'
                  : m.transport === 'stdio'
                  ? [m.command, ...(m.args || [])].filter(Boolean).join(' ')
                  : m.url}
                {' · '}
                {m.enabled ? '启用' : '停用'}
              </div>
              {(() => {
                const st = servers.find((s) => s.name === m.name)
                if (!st) return null
                return (
                  <div className={`mcp-state ${st.ok ? 'ok' : 'bad'}`}>
                    <span className="mcp-dot" />
                    {st.ok ? `已连接 · ${st.tool_count} 个工具` : `连接失败 · ${st.error || '未知原因'}`}
                  </div>
                )
              })()}
            </div>
            <div className="cfg-actions">
              <button className="ghost-btn" onClick={() => api.updateMcpServer(m.id, { enabled: !m.enabled }).then(() => load())}>
                {m.enabled ? '停用' : '启用'}
              </button>
              <button className="ghost-btn danger" onClick={() => del(m.id)}>
                删除
              </button>
            </div>
          </div>
        ))}
      </div>

      <div className="form-grid">
        <label className="full">
          <span>mcp.json 配置（标准 mcpServers 格式）</span>
          <textarea
            className="mcp-json"
            value={jsonText}
            spellCheck={false}
            onChange={(e) => {
              dirtyRef.current = true
              setJsonText(e.target.value)
            }}
            placeholder={EXAMPLE}
          />
        </label>
      </div>
      {error && <div className="auth-error">{error}</div>}
      {notice && <div className="mcp-notice">{notice}</div>}
      <div className="form-actions">
        <button className="new-btn small" onClick={saveJson} disabled={busy}>
          {busy ? '保存中…' : '保存（按 name 合并）'}
        </button>
        <button className="ghost-btn" onClick={formatJson}>
          格式化
        </button>
        <button className="ghost-btn" onClick={() => setJsonText(EXAMPLE)}>
          填入示例
        </button>
        <button
          className="ghost-btn"
          onClick={() => {
            dirtyRef.current = false
            load(true)
          }}
        >
          重新载入当前配置
        </button>
      </div>
    </div>
  )
}



function SkillsSection() {
  const [list, setList] = useState<Skill[]>([])
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [form, setForm] = useState({ name: '', description: '', content: '' })
  const [busy, setBusy] = useState(false)
  const [readonlyView, setReadonlyView] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [overwrite, setOverwrite] = useState(false)
  const fileRef = useRef<HTMLInputElement | null>(null)

  const load = () => api.listSkills().then(setList).catch((e) => setError(String(e)))
  useEffect(() => {
    load()
  }, [])

  const reset = () => {
    setEditing(null)
    setReadonlyView(false)
    setForm({ name: '', description: '', content: '' })
  }

  const sourceLabel = (s: Skill) =>
    s.source === 'user' ? '我的' : s.source === 'shared' ? '公用' : '内置'

  const edit = async (s: Skill) => {
    setEditing(s.name)
    setReadonlyView(s.source !== 'user')
    setForm({ name: s.name, description: s.description, content: '' })
    try {
      const detail = await api.getSkill(s.name)
      setForm({ name: detail.name, description: detail.description, content: detail.content })
    } catch (e: any) {
      setError(e?.message || String(e))
    }
  }

  const save = async () => {
    setError(null)
    setNotice(null)
    if (!form.name.trim()) {
      setError('Skill 名称不能为空')
      return
    }
    setBusy(true)
    try {
      if (editing) {
        const body: any = { name: form.name, description: form.description }
        if (form.content) body.content = form.content
        await api.updateSkill(editing, body)
      } else {
        await api.createSkill({ name: form.name, description: form.description, content: form.content })
      }
      reset()
      await load()
    } catch (e: any) {
      setError(e?.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const del = async (name: string) => {
    if (!confirm(`删除 Skill「${name}」？`)) return
    try {
      await api.deleteSkill(name)
      await load()
    } catch (e: any) {
      setError(e?.message || String(e))
    }
  }

  // 上传 zip 技能包（可含多个 Skill 目录及 scripts / references 等附属文件）
  const upload = async (file: File | null | undefined) => {
    if (!file) return
    setError(null)
    setNotice(null)
    setUploading(true)
    try {
      const r = await api.importSkillZip(file, overwrite)
      setNotice(`已导入 ${r.installed.length} 个 Skill：${r.installed.join('、')}`)
      await load()
    } catch (e: any) {
      setError(e?.message || String(e))
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  return (
    <div className="settings-section">
      <p className="hint">
        Skill 是可复用的操作手册（<code>SKILL.md</code>）。系统会把 Skill 名称与描述注入模型，
        模型在任务匹配时会自动调用 <code>load_skill</code> 加载完整说明。
        「公用 / 内置」只读；同名的「我的 Skill」会覆盖它们。
      </p>

      {/* ---- zip 技能包导入 ---- */}
      <div className="skill-drop">
        <div className="skill-drop-main">
          <b>导入技能包（.zip）</b>
          <div className="hint" style={{ margin: '2px 0 0' }}>
            支持完整目录结构：可含 <code>scripts/</code>、<code>references/</code>、模板等附属文件。
            zip 根目录放 <code>SKILL.md</code>，或每个一级子目录各放一个。
          </div>
        </div>
        <div className="skill-drop-actions">
          <label className="skill-overwrite">
            <input type="checkbox" checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)} />
            覆盖同名
          </label>
          <input
            ref={fileRef}
            type="file"
            accept=".zip,application/zip"
            style={{ display: 'none' }}
            onChange={(e) => upload(e.target.files?.[0])}
          />
          <button className="new-btn small" disabled={uploading} onClick={() => fileRef.current?.click()}>
            {uploading ? '导入中…' : '选择 zip 导入'}
          </button>
        </div>
      </div>

      <div className="cfg-list">
        {list.length === 0 && <div className="hint">暂无 Skill</div>}
        {list.map((s) => (
          <div className="cfg-item" key={`${s.source}:${s.name}`}>
            <div>
              <b>{s.name}</b>{' '}
              <span className={'tag ' + (s.source === 'user' ? 'tag-user' : 'tag-ro')}>{sourceLabel(s)}</span>
              {!!s.files && s.files > 1 && (
                <span className="tag tag-files">{s.files} 个文件</span>
              )}
              <div className="cfg-meta">
                {s.description || '（无描述）'} · {s.chars} 字符
              </div>
            </div>
            <div className="cfg-actions">
              {s.source === 'user' && (
                <>
                  <button className="ghost-btn" onClick={() => edit(s)}>
                    编辑
                  </button>
                  <button className="ghost-btn danger" onClick={() => del(s.name)}>
                    删除
                  </button>
                </>
              )}
              {s.source !== 'user' && (
                <button className="ghost-btn" onClick={() => edit(s)}>
                  查看
                </button>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="form-grid">
        <label>
          <span>名称（小写字母/数字/-/_/.)</span>
          <input
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="如 pdf-report"
            disabled={!!editing || readonlyView}
          />
        </label>
        <label>
          <span>描述（一句话，决定模型是否选用它）</span>
          <input
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            placeholder="如 把数据整理成 PDF 报告"
            disabled={readonlyView}
          />
        </label>
        <label className="full">
          <span>
            SKILL.md 正文（Markdown，不含 frontmatter）
            {editing && form.name !== editing ? '' : ''}
          </span>
          <textarea
            className="mcp-json"
            value={form.content}
            spellCheck={false}
            disabled={readonlyView}
            readOnly={readonlyView}
            onChange={(e) => setForm({ ...form, content: e.target.value })}
            placeholder={'# 步骤 / 1. ... / 2. ...'}
          />
        </label>
      </div>
      {error && <div className="auth-error">{error}</div>}
      {notice && <div className="mcp-notice">{notice}</div>}
      {readonlyView && (
        <div className="skill-readonly-note">
          这是只读 Skill。如需修改，请在下方以相同名称「新增 Skill」，你的私有版本会覆盖它。
        </div>
      )}
      <div className="form-actions">
        {!readonlyView && (
          <button className="new-btn small" onClick={save} disabled={busy}>
            {busy ? '保存中…' : editing ? '保存修改' : '新增 Skill'}
          </button>
        )}
        {editing && (
          <button className="ghost-btn" onClick={reset}>
            {readonlyView ? '关闭' : '取消'}
          </button>
        )}
      </div>
    </div>
  )
}
