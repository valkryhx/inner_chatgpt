import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface Props {
  text: string
}

const SAFE_PROTOCOLS = new Set(['http:', 'https:', 'data:', 'mailto:'])

function safeUrl(url?: string): string {
  if (!url) return ''
  try {
    const u = new URL(url, window.location.origin)
    return SAFE_PROTOCOLS.has(u.protocol) ? u.toString() : ''
  } catch {
    return ''
  }
}

export default function Markdown({ text }: Props) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
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
          img: ({ src, alt }) => {
            const safe = safeUrl(typeof src === 'string' ? src : undefined)
            if (!safe) return <span className="md-img-blocked">[图片已拦截] {alt}</span>
            return <img src={safe} alt={alt || ''} loading="lazy" />
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
