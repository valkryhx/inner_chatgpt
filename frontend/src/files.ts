/**
 * Workspace 文件下载：判断本地路径、带鉴权拉取、按服务端文件名保存。
 *
 * 背景：<a download> 无法携带 Authorization，SPA 直接访问 workspace 相对路径
 * 会命中 index.html。因此所有本地文件都先用 Bearer token fetch 成 blob，
 * 再交给临时 <a download>。图片仍走 /workspace-image 内联渲染。
 */
import { getToken } from './api'

const REMOTE_PROTOCOLS = new Set(['http:', 'https:', 'data:', 'blob:'])
const WINDOWS_ABS_RE = /^[A-Za-z]:[\/]/
const LOCAL_PREFIXES = ['/workspace/', 'workspace/']
const IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|bmp|avif|svg)$/i

/** 是否应走后端 workspace 鉴权接口（图片与文件共用这一判定）。 */
export function isWorkspaceLocal(src: string): boolean {
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
  return !scheme
}

export function isImagePath(src: string): boolean {
  const s = (src || '').split(/[?#]/)[0].trim()
  return IMAGE_EXT_RE.test(s)
}

export function isWorkspaceImage(src: string): boolean {
  return isWorkspaceLocal(src) && isImagePath(src)
}

/** 把 file URI 解成后端可解析的路径字符串（其他形式原样返回）。 */
export function workspacePathOf(src: string): string {
  const s = (src || '').trim()
  if (/^file:/i.test(s)) {
    try {
      const u = new URL(s)
      return decodeURIComponent(u.pathname || '')
    } catch {
      return s
    }
  }
  // 模型常把中文文件名写成 URL 编码（reports/%E5%AD%A3...docx），
  // 若原样传给后端会命中不到文件；这里还原一次，并去掉查询/锚点。
  let out = s.split(/[?#]/)[0]
  if (/%[0-9A-Fa-f]{2}/.test(out)) {
    try {
      out = decodeURIComponent(out)
    } catch {
      /* 含非法转义时保持原样 */
    }
  }
  return out
}

export function basename(path: string): string {
  const p = (path || '').replace(/[\\/]+$/, '')
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'))
  return i >= 0 ? p.slice(i + 1) : p
}

export type FileKind = 'doc' | 'sheet' | 'slide' | 'pdf' | 'img' | 'code' | 'text' | 'data' | 'zip' | 'file'

export function fileKind(name: string): FileKind {
  const ext = (/\.([A-Za-z0-9]+)$/.exec(name || '')?.[1] || '').toLowerCase()
  if (['doc', 'docx', 'odt', 'rtf'].includes(ext)) return 'doc'
  if (['xls', 'xlsx', 'ods', 'csv', 'tsv'].includes(ext)) return 'sheet'
  if (['ppt', 'pptx', 'odp'].includes(ext)) return 'slide'
  if (ext === 'pdf') return 'pdf'
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'avif', 'svg'].includes(ext)) return 'img'
  if (['py', 'js', 'ts', 'tsx', 'jsx', 'go', 'java', 'c', 'cpp', 'rs', 'sh', 'ps1', 'sql', 'json', 'yaml', 'yml'].includes(ext)) return 'code'
  if (['md', 'txt', 'log'].includes(ext)) return 'text'
  if (['zip', '7z', 'rar', 'tar', 'gz'].includes(ext)) return 'zip'
  if (['bin', 'dat', 'db', 'parquet'].includes(ext)) return 'data'
  return 'file'
}

/** 解析 Content-Disposition（优先 RFC 5987 的 filename*）。 */
export function filenameFromDisposition(disp: string | null): string {
  if (!disp) return ''
  const star = /filename\*\s*=\s*(?:utf-8|UTF-8)''([^;]+)/.exec(disp)
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ''))
    } catch {
      /* fall through */
    }
  }
  const plain = /filename\s*=\s*"([^"]+)"/.exec(disp) || /filename\s*=\s*([^;]+)/.exec(disp)
  return plain ? plain[1].trim().replace(/^"|"$/g, '') : ''
}

async function errorText(res: Response): Promise<string> {
  if (res.status === 401) return '登录已过期，请重新登录后再下载'
  if (res.status === 403) return '没有权限读取该文件'
  if (res.status === 404) return '文件不存在或已被删除'
  if (res.status === 413) return '文件超过下载上限'
  try {
    const body = await res.json()
    const detail = body?.detail ?? body?.error
    if (typeof detail === 'string' && detail) return detail
  } catch {
    /* ignore */
  }
  return `下载失败（${res.status}）`
}

async function saveBlob(res: Response, fallbackName: string): Promise<void> {
  if (!res.ok) throw new Error(await errorText(res))
  const name = filenameFromDisposition(res.headers.get('content-disposition')) || fallbackName
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
  // 大文件下载在部分浏览器里仍需保留一段时间，避免 url 提前失效。
  setTimeout(() => URL.revokeObjectURL(url), 30_000)
}

export async function downloadWorkspaceFile(path: string, fallbackName?: string): Promise<void> {
  const token = getToken()
  const res = await fetch('/api/me/workspace-file?path=' + encodeURIComponent(path), {
    headers: token ? { authorization: 'Bearer ' + token } : undefined,
  })
  await saveBlob(res, fallbackName || basename(path) || 'download')
}

export async function downloadWorkspaceZip(paths: string[]): Promise<void> {
  const token = getToken()
  const res = await fetch('/api/me/workspace-zip', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      ...(token ? { authorization: 'Bearer ' + token } : {}),
    },
    body: JSON.stringify({ paths }),
  })
  await saveBlob(res, 'workspace.zip')
}
