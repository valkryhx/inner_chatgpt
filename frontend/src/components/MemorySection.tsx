import { useEffect, useState } from 'react'
import { api } from '../api'
import type { MemoryEntry, MemoryStatus } from '../types'

/** 记忆类型的中文标签（与后端 memory_store.KINDS 对齐）。 */
const MEMORY_KINDS: Array<{ value: string; label: string; hint: string }> = [
  { value: 'user', label: '关于我', hint: '称呼、角色、语气、长期偏好' },
  { value: 'feedback', label: '工作方式', hint: '你对我的纠正，以及你确认过的做法' },
  { value: 'project', label: '项目状态', hint: '跨会话的项目进度、遗留问题' },
  { value: 'reference', label: '外部资源', hint: 'URL、看板、工单号等指针' },
]

function kindLabel(kind: string): string {
  return MEMORY_KINDS.find((k) => k.value === kind)?.label || kind
}

function fmtDay(ms: number): string {
  if (!ms) return ''
  const d = new Date(ms)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

const EMPTY_FORM = {
  slug: '',
  title: '',
  description: '',
  content: '',
  kind: 'user',
  tags: '',
  version: '',
}

/**
 * 我的记忆：查看 / 编辑 / 删除。
 *
 * 为什么这个 tab 是「自动提取」的前置条件（不是可选项）：
 * 自动写入是整个记忆系统里唯一会**自己长东西**的机制。用户看不见内容、
 * 又没有删除入口时，一批来路不明的文本会被注入进每个会话 —— 这个组合不能上线。
 * 所以后端加自动提取之前，这里必须先存在。
 */
export default function MemorySection() {
  const [status, setStatus] = useState<MemoryStatus | null>(null)
  const [list, setList] = useState<MemoryEntry[]>([])
  const [remaining, setRemaining] = useState(0)
  const [nextCursor, setNextCursor] = useState('')
  const [total, setTotal] = useState(0)
  const [query, setQuery] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // 编辑态：editing 为空 = 新建
  const [editing, setEditing] = useState<string | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [form, setForm] = useState(EMPTY_FORM)

  const loadStatus = () =>
    api.memoryStatus().then(setStatus).catch((e) => setError(String(e)))

  const load = async (cursor = '', append = false) => {
    try {
      const page = await api.listMemories(cursor, 50, query)
      setList((prev) => (append ? [...prev, ...page.entries] : page.entries))
      setRemaining(page.remaining)
      setNextCursor(page.next_cursor)
      setTotal(page.total)
    } catch (e: any) {
      setError(e?.message || String(e))
    }
  }

  useEffect(() => {
    loadStatus()
    load()
    // 只在挂载时拉一次；筛选走 onSearch，避免每次输入都打接口
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const reset = () => {
    setEditing(null)
    setDetailOpen(false)
    setForm(EMPTY_FORM)
  }

  const openEdit = async (slug: string) => {
    setError(null)
    setNotice(null)
    setBusy(true)
    try {
      const d = await api.getMemory(slug)
      setEditing(d.slug)
      setDetailOpen(true)
      setForm({
        slug: d.slug,
        title: d.title,
        description: d.description,
        content: d.content,
        kind: d.kind,
        tags: d.tags.join(', '),
        version: d.version,
      })
    } catch (e: any) {
      setError(e?.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const save = async () => {
    setError(null)
    setNotice(null)
    if (!editing && !form.content.trim()) {
      setError('正文不能为空')
      return
    }
    setBusy(true)
    const tags = form.tags.split(/[,，;；]/).map((t) => t.trim()).filter(Boolean)
    try {
      if (editing) {
        // 带上 version 做 CAS：如果后台刚改过同一条，后端返回 409 而不是静默覆盖
        await api.updateMemory(editing, {
          title: form.title,
          description: form.description,
          content: form.content,
          kind: form.kind,
          tags,
          if_version: form.version,
        })
        setNotice('已保存')
      } else {
        await api.createMemory({
          slug: form.slug,
          title: form.title,
          description: form.description,
          content: form.content,
          kind: form.kind,
          tags,
        })
        setNotice('已新增')
      }
      await Promise.all([load(), loadStatus()])
      reset()
    } catch (e: any) {
      const msg = e?.message || String(e)
      setError(
        msg.includes('409') || msg.includes('conflict')
          ? '这条记忆刚被改过（可能是模型写入的）。已取消保存，避免覆盖对方的内容；请重新打开后再改。'
          : msg,
      )
    } finally {
      setBusy(false)
    }
  }

  const del = async (slug: string) => {
    if (!confirm(`删除记忆「${slug}」？删除后不会再被注入到对话里。`)) return
    setError(null)
    try {
      await api.deleteMemory(slug)
      if (editing === slug) reset()
      await Promise.all([load(), loadStatus()])
      setNotice('已删除')
    } catch (e: any) {
      setError(e?.message || String(e))
    }
  }

  const onSearch = async () => {
    setError(null)
    await load('', false)
  }

  return (
    <div className="settings-section">
      <p className="hint">
        记忆是跨会话保留的长期事实：你的偏好、称呼、对工作方式的纠正、项目状态。
        它们会按相关性自动注入到对话上下文里（<b>不会</b>逐条念给你听）。
        记忆按用户隔离，别人看不到你的。
      </p>

      {status && (
        <div className="memory-status">
          <div>
            <b>{status.total}</b> 条记忆
            <span className="memory-budget">
              每轮注入预算 {status.inject_budget} 字符 / 最多 {status.inject_max} 条
            </span>
          </div>
          <div className="memory-flags">
            <span className={'tag ' + (status.enabled ? 'tag-user' : 'tag-ro')}>
              {status.enabled ? '记忆已启用' : '记忆已关闭'}
            </span>
            <span className={'tag ' + (status.auto_write ? 'tag-user' : 'tag-ro')}>
              {status.auto_write ? '自动提取已开' : '自动提取已关'}
            </span>
            {status.disabled_by_env && <span className="tag tag-ro">环境变量已停用后台任务</span>}
          </div>
        </div>
      )}

      {status && !status.auto_write && (
        <div className="memory-notice">
          自动提取当前<b>关闭</b>：只有我在对话中主动调用 <code>memory_write</code>
          时才会新增记忆，而且你能当轮看到那一步工具调用。想让模型「自己记」需要显式开启
          <code>HLZ_MEMORY_AUTO_WRITE=1</code>。
        </div>
      )}

      {error && <div className="error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}

      <div className="memory-search">
        <input
          value={query}
          placeholder="按名称 / 描述 / 标签筛选"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onSearch()
          }}
        />
        <button className="ghost-btn" onClick={onSearch}>筛选</button>
        <button
          className="new-btn small"
          onClick={() => {
            reset()
            setDetailOpen(true)
          }}
        >
          新增记忆
        </button>
      </div>

      <div className="cfg-list">
        {list.length === 0 && <div className="hint">还没有记忆。</div>}
        {list.map((m) => (
          <div className="cfg-item memory-item" key={m.slug}>
            <div className="memory-item-main">
              <div>
                <b>{m.title || m.slug}</b>{' '}
                <span className="tag tag-kind">{kindLabel(m.kind)}</span>
                {m.tags.map((t) => (
                  <span className="tag tag-files" key={t}>{t}</span>
                ))}
              </div>
              <div className="hint memory-desc">{m.description || '（无描述）'}</div>
              <div className="memory-meta">
                <code>{m.slug}</code> · 更新于 {fmtDay(m.updated_at)} · {m.chars} 字
              </div>
            </div>
            <div className="cfg-actions">
              <button className="ghost-btn" onClick={() => openEdit(m.slug)} disabled={busy}>
                编辑
              </button>
              <button className="ghost-btn danger" onClick={() => del(m.slug)}>删除</button>
            </div>
          </div>
        ))}
        {remaining > 0 && (
          <button className="ghost-btn memory-more" onClick={() => load(nextCursor, true)}>
            还有 {remaining} 条，继续加载
          </button>
        )}
        {remaining === 0 && list.length > 0 && (
          <div className="hint memory-total">共 {total} 条，已全部显示</div>
        )}
      </div>

      {detailOpen && (
        <div className="memory-editor">
          <div className="memory-editor-head">
            <b>{editing ? `编辑：${editing}` : '新增记忆'}</b>
            <button className="icon-btn" onClick={reset} aria-label="关闭">✕</button>
          </div>
          {!editing && (
            <label>
              标识（slug）
              <input
                value={form.slug}
                placeholder="留空则从标题生成，如 user-name-pref"
                onChange={(e) => setForm({ ...form, slug: e.target.value })}
              />
            </label>
          )}
          <label>
            标题
            <input
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
            />
          </label>
          <label>
            一句话描述（判断相关性的唯一依据，要具体）
            <input
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          </label>
          <label>
            类型
            <select
              value={form.kind}
              onChange={(e) => setForm({ ...form, kind: e.target.value })}
            >
              {MEMORY_KINDS.map((k) => (
                <option key={k.value} value={k.value}>{k.label} — {k.hint}</option>
              ))}
            </select>
          </label>
          <label>
            标签（逗号分隔）
            <input
              value={form.tags}
              onChange={(e) => setForm({ ...form, tags: e.target.value })}
            />
          </label>
          <label>
            正文（markdown）
            <textarea
              rows={8}
              value={form.content}
              onChange={(e) => setForm({ ...form, content: e.target.value })}
            />
          </label>
          <div className="form-actions">
            <button className="new-btn small" onClick={save} disabled={busy}>
              {busy ? '保存中…' : editing ? '保存修改' : '新增记忆'}
            </button>
            <button className="ghost-btn" onClick={reset}>取消</button>
          </div>
        </div>
      )}
    </div>
  )
}
