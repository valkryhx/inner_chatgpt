import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import MarkdownImage from './MarkdownImage'

interface Props {
  text: string
}

const SAFE_PROTOCOLS = new Set(['http:', 'https:', 'data:', 'mailto:', 'blob:'])

function safeUrl(url?: string): string {
  if (!url) return ''
  try {
    const u = new URL(url, window.location.origin)
    return SAFE_PROTOCOLS.has(u.protocol) ? u.toString() : ''
  } catch {
    return ''
  }
}

/**
 * react-markdown 默认只允许 http(s)/ircs/mailto/xmpp，会把：
 * - `D:\...` 当成未知协议而清空；
 * - `chart.png` 保留为相对 URL，但 <img> 无法带鉴权。
 *
 * 这里放宽「非协议型相对路径」与 file URI，真正的安全边界由
 * MarkdownImage 的 workspace 鉴权接口负责（路径越界会被后端 403）。
 */
function safeMarkdownUrl(url?: string): string {
  const s = (url || '').trim()
  if (!s) return ''
  if (/^file:/i.test(s)) return s
  if (/^[A-Za-z]:[\/]/.test(s)) return s
  if (s.startsWith('/') || s.startsWith('./') || s.startsWith('../')) return s
  if (/^[a-z][a-z0-9+.-]*:/i.test(s)) {
    const safe = safeUrl(s)
    return safe
  }
  return s
}

export default function Markdown({ text }: Props) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={safeMarkdownUrl}
        components={{
          a: ({ href, children, ...rest }) => {
            const safe = safeUrl(href)
            if (!safe) return <span>{children}</span>
            return (
              <a href={safe} target="_blank" rel="noreferrer noopener" {...rest}>
                {children}
              </a>
            )
          },
          img: ({ src, alt, title }) => {
            const raw = typeof src === 'string' ? src : ''
            if (!raw) return <span className="md-img-blocked">[图片地址为空] {alt}</span>
            return <MarkdownImage src={raw} alt={alt} title={title} />
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
