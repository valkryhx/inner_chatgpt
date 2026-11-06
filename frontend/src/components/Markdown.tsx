import ReactMarkdown from 'react-markdown'
import rehypeRaw from 'rehype-raw'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'
import MarkdownImage from './MarkdownImage'
import FileChip from './FileChip'
import { basename, isWorkspaceLocal, workspacePathOf } from '../files'

interface Props {
  text: string
  onImageSettled?: () => void
}

const SAFE_PROTOCOLS = new Set(['http:', 'https:', 'data:', 'mailto:', 'blob:'])
const DRIVE_PROTOCOLS = [
  ...'abcdefghijklmnopqrstuvwxyz'.split(''),
  ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split(''),
]

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
  if (/^[a-z][a-z0-9+.-]*:/i.test(s)) return safeUrl(s)
  return s
}

/**
 * 模型经常直接写原生 HTML <img>（尤其从别的平台迁移过来的习惯）。
 * 默认 react-markdown 会丢掉原始 HTML，所以这里：
 * 1) rehype-raw 把 HTML 解析成节点；
 * 2) rehype-sanitize 只保留安全子集；
 * 3) 额外放行图片所需的 src/alt/title/宽高，以及 details/summary。
 *
 * 脚本、事件属性、iframe、style/class 等仍被剔除，安全边界不降级。
 */
const schema = {
  ...defaultSchema,
  tagNames: [
    ...(defaultSchema.tagNames || []),
    'img', 'figure', 'figcaption', 'details', 'summary', 'kbd', 'mark',
  ],
  attributes: {
    ...defaultSchema.attributes,
    img: [...(defaultSchema.attributes?.img || []), 'src', 'alt', 'title', 'width', 'height'],
    a: [...(defaultSchema.attributes?.a || []), 'href', 'title'],
    details: ['open'],
  },
  protocols: {
    ...defaultSchema.protocols,
    // 单字母协议只用于 Windows 盘符（D:/...）；javascript/vbscript 等仍被拒绝。
    src: ['http', 'https', 'data', 'file', 'blob', ...DRIVE_PROTOCOLS],
  },
}

export default function Markdown({ text, onImageSettled }: Props) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw, [rehypeSanitize, schema]]}
        urlTransform={safeMarkdownUrl}
        components={{
          a: ({ href, children, ...rest }) => {
            const raw = (href || '').trim()
            // 本地 workspace 文件（含图片）→ 文件胶囊 + 鉴权下载。
            // 链接不会内联渲染（那是 ![alt](path) 的职责），若按普通 <a> 处理，
            // 点击只会跳到 SPA 路由拿到 index.html，因此图片链接同样给下载入口。
            if (raw && isWorkspaceLocal(raw)) {
              const path = workspacePathOf(raw)
              return <FileChip path={path} name={basename(path)} label={children} />
            }
            const safe = safeUrl(href)
            if (!safe) return <span>{children}</span>
            return (
              <a href={safe} target="_blank" rel="noreferrer noopener" {...rest}>
                {children}
              </a>
            )
          },
          img: ({ src, alt, title, width, height }) => {
            const raw = typeof src === 'string' ? src : ''
            if (!raw) return <span className="md-img-blocked">[图片地址为空] {alt}</span>
            return (
              <MarkdownImage
                src={raw}
                alt={alt}
                title={title}
                width={width}
                height={height}
                onSettled={onImageSettled}
              />
            )
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
