import { useEffect, useState } from 'react'
import { api } from '../api'
import type { A2AAgentInfo, A2AToken, A2ATokenCreated } from '../types'

function fmt(ms?: number | null) {
  if (!ms) return '—'
  return new Date(ms).toLocaleString()
}

export default function A2aSection() {
  const [agent, setAgent] = useState<A2AAgentInfo | null>(null)
  const [tokens, setTokens] = useState<A2AToken[]>([])
  const [name, setName] = useState('claude-code')
  const [days, setDays] = useState('90')
  const [policy, setPolicy] = useState('full')
  const [created, setCreated] = useState<A2ATokenCreated | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = async () => {
    setError(null)
    try {
      const [a, t] = await Promise.all([api.getA2AAgent(), api.listA2ATokens()])
      setAgent(a)
      setTokens(t.tokens)
    } catch (e: any) {
      setError(e?.message || String(e))
    }
  }

  useEffect(() => { load() }, [])

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setNotice('已复制到剪贴板')
      setTimeout(() => setNotice(null), 1800)
    } catch {
      setNotice('复制失败，请手动选择文本')
    }
  }

  const create = async () => {
    setBusy(true)
    setError(null)
    setCreated(null)
    try {
      const payload: {
        name?: string
        scopes: string[]
        expires_in_days?: number | null
        tool_policy?: string
      } = {
        name: name.trim() || 'a2a-client',
        scopes: ['chat'],
        tool_policy: policy,
      }
      payload.expires_in_days = days.trim() === '' ? null : Math.max(1, parseInt(days, 10) || 90)
      const token = await api.createA2AToken(payload)
      setCreated(token)
      await load()
    } catch (e: any) {
      setError(e?.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const toggle = async () => {
    if (!agent) return
    setBusy(true)
    setError(null)
    try {
      const next = await api.setA2AEnabled(!agent.enabled)
      setAgent(next)
      setNotice(next.enabled ? 'A2A 已开启，外部可调用' : 'A2A 已关闭，外部调用一律 404')
      setTimeout(() => setNotice(null), 2200)
    } catch (e: any) {
      setError(e?.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (id: string) => {
    if (!confirm('撤销这个 token？撤销后立即失效，不可恢复。')) return
    try {
      await api.revokeA2AToken(id)
      await load()
    } catch (e: any) {
      setError(e?.message || String(e))
    }
  }

  if (error && !agent) {
    return (
      <div className="settings-section">
        <p className="hint">
          A2A 当前不可用：{error}
          <br />
          如果后端未启用，请用 <code>INNER_A2A_ENABLED=true</code> 重启服务。
        </p>
      </div>
    )
  }

  return (
    <div className="settings-section">
      <p className="hint">
        每个用户有独立的 A2A 身份、URL 和 Bearer token。外部智能体（例如 Claude Code +
        <code>a2a</code> CLI）用它调用你的 Hlaudz。token 只显示一次，后端只保存哈希。
      </p>

      {agent && (
        <div className="cfg-item">
          <div>
            <b>A2A Endpoint</b>
            <div className="cfg-meta">{agent.url}</div>
            <div className="cfg-meta">Card: {agent.card_url}</div>
            <div className="cfg-meta">Tenant: {agent.tenant}</div>
          </div>
          <div className="cfg-actions">
            <button className="ghost-btn" onClick={() => copy(agent.url)}>复制 URL</button>
            <button className="ghost-btn" onClick={() => copy(agent.card_url)}>复制 Card</button>
          </div>
        </div>
      )}

      {agent && (
        <div className={'a2a-switch' + (agent.enabled ? ' on' : '')}>
          <div className="a2a-switch-main">
            <b>{agent.enabled ? 'A2A 已开启' : 'A2A 未开启'}</b>
            <div className="cfg-meta">
              {agent.enabled
                ? '持有 token 的外部智能体可以调用你；随时可以关掉。'
                : '不会响应任何外部调用（卡片与 JSON-RPC 都是 404），已有 token 保留。'}
            </div>
          </div>
          <button
            type="button"
            className={'switch' + (agent.enabled ? ' on' : '')}
            onClick={toggle}
            disabled={busy}
            aria-label={agent.enabled ? '关闭 A2A' : '开启 A2A'}
            aria-pressed={agent.enabled}
            title={agent.enabled ? '点击关闭' : '点击开启'}
          >
            <span className="knob" />
          </button>
        </div>
      )}

      <div className="form-grid">
        <label>
          <span>Token 名称</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="claude-code" />
        </label>
        <label>
          <span>有效期（天，空=不过期）</span>
          <input type="number" min={1} value={days} onChange={(e) => setDays(e.target.value)} />
        </label>
        <label className="full">
          <span>能力档位（建 token 时定死，运行期不再弹审批）</span>
          <select value={policy} onChange={(e) => setPolicy(e.target.value)}>
            {(agent?.tool_policies && agent.tool_policies.length > 0
              ? agent.tool_policies
              : [
                  { id: 'full', description: '完整权限：内置文件/执行工具 + MCP + 子代理编排 + GA' },
                  { id: 'read_only', description: '只读：file_read / list_files / load_skill / memory_list / memory_read（不写记忆）' },
                  { id: 'chat_only', description: '纯对话：不注入任何工具' },
                ]
            ).map((p) => (
              <option key={p.id} value={p.id}>{p.id} — {p.description}</option>
            ))}
          </select>
        </label>
      </div>

      <div className="form-actions">
        <button className="new-btn small" onClick={create} disabled={busy}>
          {busy ? '创建中…' : '创建 A2A Token'}
        </button>
      </div>

      {notice && <div className="mcp-notice">{notice}</div>}
      {error && <div className="auth-error">{error}</div>}

      {created && (
        <div className="settings-section">
          <p className="hint">
            <b>新 token（只显示这一次）：</b> {created.warning}
          </p>
          <textarea className="mcp-json" readOnly value={created.token} onFocus={(e) => e.currentTarget.select()} />
          <div className="form-actions">
            <button className="ghost-btn" onClick={() => copy(created.token)}>复制 Token</button>
            <button className="ghost-btn" onClick={() => setCreated(null)}>我已保存</button>
          </div>
        </div>
      )}

      <div className="cfg-list">
        {tokens.length === 0 && <div className="hint">还没有 A2A token。</div>}
        {tokens.map((t) => {
          const revoked = !!t.revoked_at
          return (
            <div className="cfg-item" key={t.id}>
              <div>
                <b>{t.name || '(unnamed)'}</b>
                {revoked && <span className="tag">已撤销</span>}
                <div className="cfg-meta">
                  {t.prefix}… · scopes: {(t.scopes || []).join(', ') || '—'}
                  {' · '}能力 {t.tool_policy || 'full'} · 创建 {fmt(t.created_at)}
                  {' · '}最近使用 {fmt(t.last_used_at)}
                  {t.expires_at ? ` · 过期 ${fmt(t.expires_at)}` : ' · 不过期'}
                </div>
              </div>
              <div className="cfg-actions">
                {!revoked && (
                  <button className="ghost-btn danger" onClick={() => revoke(t.id)}>撤销</button>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
