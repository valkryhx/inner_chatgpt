import { useEffect, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent } from 'react'
import { api } from '../api'
import type { LLMConfig, MCPServer, McpServerStatus, Skill } from '../types'
import A2aSection from './A2aSection'

interface Props {
  onClose: () => void
  onLlmChanged?: () => void
}

// 模型名必须来自端点自己的 /models。这里不再维护硬编码候选列表：
// 曾经的 MODEL_PRESETS 用 qwen3-max / deepseek-flash 这类短名，在湛卢上
// 一个都不存在（真实 ID 是 moma/qwen3.7-max），选中后调用被网关以
// 403「权限不足」拒绝，用户会误以为 key 没权限。
const MODEL_CUSTOM = '__custom__'

const emptyLlm = {
  label: '',
  wire_api: 'openai_responses',
  base_url: 'https://api.deepseek.com',
  api_key: '',
  // 留空：模型名必须由端点 /models 决定。预填一个猜的名字会让人
  // 直接保存下去，然后在调用时撞 403/404。
  model: '',
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
  // 从端点 /models 查到的真实模型列表。null = 还没查过。
  const [remoteModels, setRemoteModels] = useState<{ id: string; owned_by: string }[] | null>(null)
  const [probing, setProbing] = useState(false)
  const [probeError, setProbeError] = useState<string | null>(null)
  // 已经为哪个 (base_url, key) 组合拉过列表。同一组合不重复请求——
  // 探测要几秒，用户反复点开下拉不该反复打端点。
  const probedRef = useRef('')
  // 思考等级探测结果。null = 还没测过。
  const [effortResult, setEffortResult] = useState<
    { supported: boolean | null; levels: string[]; error: string; hint: string } | null
  >(null)
  const [effortProbing, setEffortProbing] = useState(false)

  const probeReasoning = async () => {
    setEffortProbing(true)
    setEffortResult(null)
    try {
      const r = await api.probeLlmReasoning({
        base_url: form.base_url || '',
        api_key: form.api_key || '',
        config_id: editingId && !form.api_key ? editingId : '',
        model: String(form.model || ''),
        wire_api: form.wire_api || '',
      })
      setEffortResult(r)
    } catch (e: any) {
      setEffortResult({ supported: null, levels: [], error: e?.message || String(e), hint: '' })
    } finally {
      setEffortProbing(false)
    }
  }

  const load = () => api.listLlmConfigs().then(setList).catch((e) => setError(String(e)))
  useEffect(() => {
    load()
  }, [])

  const reset = () => {
    setForm({ ...emptyLlm })
    setEditingId(null)
    setCustomModel(false)
    setRemoteModels(null)
    setProbeError(null)
  }

  // 用当前的 base_url + key 去问端点自己有哪些模型。
  // key 为空且正在编辑既有配置时，后端会用库里存的那个（只按 user_id 取）。
  const probeModels = async (opts: { auto?: boolean } = {}) => {
    const auto = !!opts.auto
    const sig = `${form.base_url}||${form.api_key}||${editingId || ''}`
    if (auto && probedRef.current === sig) return
    setProbing(true)
    setProbeError(null)
    try {
      const r = await api.probeLlmModels({
        base_url: form.base_url || '',
        api_key: form.api_key || '',
        config_id: editingId && !form.api_key ? editingId : '',
      })
      if (r.ok) {
        probedRef.current = sig   // 只有成功才钉住，失败要允许下次重试
        setRemoteModels(r.models)
        // 当前填的值不在真实列表里时，提示出来（这是 403 的直接成因）
        if (form.model && !r.models.some((m) => m.id === form.model)) {
          setProbeError(
            `当前填的「${form.model}」不在该端点的模型列表里，实际调用会被拒绝。请从下方重新选择。`,
          )
        }
      } else {
        // 失败必须让用户看见。曾经这里对「自动探测」静默处理，
        // 结果端点不支持 /models（或后端没重启）时，用户只看到几条旧选项，
        // 完全不知道列表没拉全——正是「明明该有 30+ 个模型却只有 3-4 个」的成因。
        setRemoteModels(null)
        setProbeError(`${r.error}${r.hint ? ' —— ' + r.hint : ''}`)
      }
    } catch (e: any) {
      setRemoteModels(null)
      setProbeError(e?.message || String(e))
    } finally {
      setProbing(false)
    }
  }

  // 输入停手 700ms 就预取：用户还没点开下拉，列表已经在路上。
  // 探测本身要几秒，等点开再查会让下拉里先是空的。
  useEffect(() => {
    const base = (form.base_url || '').trim()
    const key = form.api_key || ''
    if (!base) return
    if (!key && !editingId) return
    const t = setTimeout(() => { probeModels({ auto: true }) }, 700)
    return () => clearTimeout(t)
    // 只在端点/密钥变化时重新预取；form.model 等其它字段变化不该重打端点
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form.base_url, form.api_key, editingId])

  const save = async () => {
    setError(null)
    // 模型名为空时保存下去，要到真正对话才炸，且报错位置离这里很远。
    if (!String(form.model || '').trim()) {
      setError('请先选择或填写模型名（点开「模型名」下拉即可获取端点真实模型）')
      return
    }
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
    setRemoteModels(null)
    setProbeError(null)
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

  // 下拉选项来源，按优先级：
  //   1. 从端点 /models 查到的真实列表（唯一可靠来源）
  //   2. 用户已保存的配置里出现过的模型名（保证编辑旧配置不丢值）
  //   3. 当前表单里的值
  // 硬编码的 MODEL_PRESETS 已不再作为「可选项」直接呈现——它们的短名在
  // 具体网关上常常不存在（湛卢要 moma/ 前缀），选了就会 403。
  const modelOptions = (() => {
    const byValue = new Map<string, string>()
    if (remoteModels) {
      for (const m of remoteModels) byValue.set(m.id, m.owned_by ? `${m.id} · ${m.owned_by}` : m.id)
    }
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
          <input
            value={form.base_url}
            onChange={(e) => {
              setF('base_url', e.target.value)
              // 端点变了，上一个端点的模型列表立即作废——留着会把 A 家的
              // 模型名存进 B 家的配置，调用时又是 404/403。
              setRemoteModels(null)
              setProbeError(null)
            }}
          />
        </label>
        <label className="full">
          <span>API Key（{editingId ? '留空不改' : '必填'}）</span>
          <input
            type="password"
            value={form.api_key}
            onChange={(e) => setF('api_key', e.target.value)}
            // 填完 key 就把探测发出去：端点 /models 要 0.5–4s，
            // 等用户真的点开下拉再查，下拉里只会是空的。
            onBlur={() => {
              if (!remoteModels && !probing && form.base_url && form.api_key) {
                probeModels({ auto: true })
              }
            }}
            placeholder="sk-..."
          />
        </label>
        <label>
          <span>模型名</span>
          <div className="model-pick-row">
            <select
              value={modelSelectValue}
              onChange={(e) => selectModel(e.target.value)}
              onFocus={() => {
                // 用户点开下拉就按端点真实模型填充，不需要先知道要点「刷新」。
                // key 没填且不是编辑既有配置时先不查，否则焦点一进来就弹 401。
                if (!remoteModels && !probing && form.base_url
                    && (form.api_key || editingId)) {
                  probeModels({ auto: true })
                }
              }}
              onMouseDown={() => {
                // 有些环境（含 headless Chromium）点 select 不一定触发 focus，
                // 这里补一条同样的路径，保证「点开下拉」这个动作稳定触发拉取。
                if (!remoteModels && !probing && form.base_url
                    && (form.api_key || editingId)) {
                  probeModels({ auto: true })
                }
              }}
            >
              {modelOptions.length === 0 && (
                <option value={MODEL_CUSTOM}>
                  {probing ? '正在获取模型列表…' : '（填好 Base URL 与 Key 后点「获取模型」）'}
                </option>
              )}
              {modelOptions.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
              <option value={MODEL_CUSTOM}>自定义模型 ID…</option>
            </select>
            <button
              type="button"
              className="ghost-btn small"
              onClick={() => probeModels()}
              disabled={probing || !form.base_url}
              title="用当前 Base URL + API Key 拉取该端点的全部可用模型"
            >
              {probing ? '获取中…' : '获取模型'}
            </button>
          </div>
          {probeError && <span className="field-error">{probeError}</span>}
          {remoteModels && !probeError && (
            <span className="field-help ok">
              已从端点读取 {remoteModels.length} 个模型
            </span>
          )}
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
          <span className="field-help">
            模型名必须与端点 /models 返回的一致（很多网关带前缀，如 <code>moma/qwen3.7-max</code>）。
            点「获取模型」可拉取该端点的全部可用模型；端点不支持时也可手填。
          </span>
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
          <div className="effort-row">
            <input value={form.reasoning_effort} onChange={(e) => setF('reasoning_effort', e.target.value)} placeholder="可空" />
            <button
              type="button"
              className="ghost-btn small"
              onClick={probeReasoning}
              disabled={effortProbing || !String(form.model || '').trim()}
              title="发一个非法档位给端点，看它报不报错——从而判断它是否真的支持思考等级"
            >
              {effortProbing ? '探测中…' : '测支持'}
            </button>
          </div>
        </label>
        {effortResult && (
          <div className={effortResult.supported === false ? 'field-error' : 'field-hint'}>
            {effortResult.supported === true && (
              <>
                该端点<b>会校验</b> reasoning_effort。
                {effortResult.levels.length > 0
                  ? `它自报支持：${effortResult.levels.join(' / ')}`
                  : '但没在报错里列出可选档位，建议手动试档或留空。'}
              </>
            )}
            {effortResult.supported === false && (
              <>
                该端点<b>整段忽略</b> reasoning_effort：无论选哪一档都不改变上游行为。
                这是网关侧限制，不是 Hlaudz 配置问题——留空即可，选了也不会生效。
              </>
            )}
            {effortResult.supported === null && (
              <>无法判定：{effortResult.error}{effortResult.hint ? ` —— ${effortResult.hint}` : ''}</>
            )}
          </div>
        )}
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
