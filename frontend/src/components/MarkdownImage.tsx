import { useEffect, useState } from 'react'
import { getToken } from '../api'

const REMOTE_PROTOCOLS = new Set(['http:', 'https:', 'data:', 'blob:'])
const WINDOWS_ABS_RE = /^[A-Za-z]:[\/]/
const LOCAL_PREFIXES = ['/workspace/', 'workspace/']

/**
 * 判断图片地址是否应走「当前用户 workspace 鉴权接口」。
 *
 * - http(s)/data/blob 保持原样，由浏览器直接加载；
 * - 绝对 file URI、Windows 盘符路径、/workspace/... 与普通相对路径
 *   都视为 workspace 本地文件，必须带 Bearer token 拉取。
 */
export function isWorkspaceImage(src: string): boolean {
  const s = (src || '').trim()
  if (!s) return false
  if (s.startsWith('//')) return false
  try {
    const u = new URL(s, window.location.origin)
    if (REMOTE_PROTOCOLS.has(u.protocol) && u.protocol !== 'blob:') return true
  } catch {
    // continue with path heuristics
  }
  if (/^file:/i.test(s)) return true
  if (WINDOWS_ABS_RE.test(s)) return true
  if (LOCAL_PREFIXES.some((p) => s.startsWith(p))) return true
  // 以 / 开头但不是 // 的路径，按 workspace 相对路径处理（与后端一致）。
  if (s.startsWith('/')) return true
  // 其余普通相对路径。
  return !/^[a-z][a-z0-9+.-]*:/i.test(s)
}

/** 把 workspace 路径归一成后端可解析的字符串（保留盘符与原始信息）。 */
export function workspaceImagePath(src: string): string {
  const s = (src || '').trim()
  if (/^file:/i.test(s)) {
    try {
      const u = new URL(s)
      return decodeURIComponent(u.pathname || '')
    } catch {
      return s
    }
  }
  return s
}

async function fetchWorkspaceImage(src: string, signal: AbortSignal): Promise<string> {
  const token = getToken()
  const url = '/api/me/workspace-image?path=' + encodeURIComponent(workspaceImagePath(src))
  const res = await fetch(url, {
    headers: token ? { authorization: 'Bearer ' + token } : undefined,
    signal,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail || body.error || detail
    } catch {
      /* ignore */
    }
    throw new Error(detail || `图片加载失败 (${res.status})`)
  }
  const blob = await res.blob()
  return URL.createObjectURL(blob)
}

interface Props {
  src: string
  alt?: string
  title?: string
}

/**
 * Markdown 图片渲染：
 * - 远程 URL 直接用 <img>，由浏览器加载；
 * - workspace 本地路径先用带鉴权的 fetch 拉成 blob，再交给 <img>。
 *
 * 直接给 <img src="/workspace/x.png"> 在 SPA 下会拿到 index.html，
 * 且 <img> 无法携带 Authorization，因此必须走这里。
 */
export default function MarkdownImage({ src, alt, title }: Props) {
  const local = isWorkspaceImage(src)
  const [resolved, setResolved] = useState(local ? '' : src)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(local)

  useEffect(() => {
    if (!local) {
      setResolved(src)
      setError('')
      setLoading(false)
      return
    }
    const ctrl = new AbortController()
    let objectUrl = ''
    setLoading(true)
    setError('')
    fetchWorkspaceImage(src, ctrl.signal)
      .then((url) => {
        objectUrl = url
        setResolved(url)
      })
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return
        setError(err instanceof Error ? err.message : String(err))
        setResolved('')
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false)
      })
    return () => {
      ctrl.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [src, local])

  if (loading) {
    return (
      <span className="md-image md-image-loading" role="img" aria-label={alt || '图片加载中'}>
        <span className="md-image-placeholder">图片加载中…</span>
      </span>
    )
  }
  if (error) {
    return (
      <span className="md-image-blocked" role="img" aria-label={alt || '图片加载失败'}>
        [图片加载失败] {alt || src}
        <span className="md-image-error">{error}</span>
      </span>
    )
  }
  return (
    <span className="md-image">
      <img src={resolved} alt={alt || ''} title={title} loading="lazy" />
    </span>
  )
}
