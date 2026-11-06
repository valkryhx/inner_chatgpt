import { useEffect, useRef, useState } from 'react'
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
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(s)?.[1]?.toLowerCase()
  if (scheme) {
    if (REMOTE_PROTOCOLS.has(scheme + ':')) return false
    if (scheme === 'file') return true
  }
  if (WINDOWS_ABS_RE.test(s)) return true
  if (LOCAL_PREFIXES.some((p) => s.startsWith(p))) return true
  // 以 / 开头但不是 // 的路径，按 workspace 相对路径处理（与后端一致）。
  if (s.startsWith('/')) return true
  // 其余普通相对路径；未知协议不交给后端。
  return !scheme
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

// React 在流式重渲染时可能重挂载 Markdown 子树。缓存已解析的 blob URL，
// 避免同一个 workspace 图片每次挂载都先退回占位符、再撑开高度。
type WorkspaceImageEntry = { promise: Promise<string>; value?: string }
const workspaceImageCache = new Map<string, WorkspaceImageEntry>()

function cachedWorkspaceImage(src: string): string {
  return workspaceImageCache.get(workspaceImagePath(src))?.value || ''
}

async function fetchWorkspaceImage(src: string): Promise<string> {
  const key = workspaceImagePath(src)
  let entry = workspaceImageCache.get(key)
  if (!entry) {
    const promise = (async () => {
      const token = getToken()
      const url = '/api/me/workspace-image?path=' + encodeURIComponent(key)
      const res = await fetch(url, {
        headers: token ? { authorization: 'Bearer ' + token } : undefined,
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
      const objectUrl = URL.createObjectURL(blob)
      entry!.value = objectUrl
      return objectUrl
    })()
    entry = { promise }
    workspaceImageCache.set(key, entry)
    promise.catch(() => workspaceImageCache.delete(key))
  }
  return entry.promise
}

interface Props {
  src: string
  alt?: string
  title?: string
  width?: string | number
  height?: string | number
  onSettled?: () => void
}

/**
 * Markdown 图片渲染：
 * - 远程 URL 直接用 <img>，由浏览器加载；
 * - workspace 本地路径先用带鉴权的 fetch 拉成 blob，再交给 <img>。
 *
 * 直接给 <img src="/workspace/x.png"> 在 SPA 下会拿到 index.html，
 * 且 <img> 无法携带 Authorization，因此必须走这里。
 */
export default function MarkdownImage({ src, alt, title, width, height, onSettled }: Props) {
  const local = isWorkspaceImage(src)
  const cached = local ? cachedWorkspaceImage(src) : src
  const [resolved, setResolved] = useState(cached)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(local && !cached)
  const settledRef = useRef(false)

  const notifySettled = () => {
    if (settledRef.current) return
    settledRef.current = true
    // rAF 让真实图片尺寸先进入布局，再由 MessageList 判断是否继续跟随底部。
    requestAnimationFrame(() => onSettled?.())
  }

  useEffect(() => {
    settledRef.current = false
    let cancelled = false

    if (!local) {
      setResolved(src)
      setError('')
      setLoading(false)
      return () => {
        cancelled = true
      }
    }

    const hit = cachedWorkspaceImage(src)
    if (hit) {
      setResolved(hit)
      setLoading(false)
      return () => {
        cancelled = true
      }
    }
    setLoading(true)
    setError('')
    fetchWorkspaceImage(src)
      .then((url) => {
        if (cancelled) return
        setResolved(url)
        setLoading(false)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : String(err))
        setResolved('')
        setLoading(false)
        notifySettled()
      })
    return () => {
      cancelled = true
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
      <img
        src={resolved}
        alt={alt || ''}
        title={title}
        width={width}
        height={height}
        loading="lazy"
        onLoad={notifySettled}
        onError={notifySettled}
      />
    </span>
  )
}
