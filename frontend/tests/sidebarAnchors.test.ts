/**
 * 侧边栏两个「导航锚点」必须与会话条目在视觉上区分开。
 *
 * ## 问题（用户报告）
 *
 * 「对话和任务」与「查看全部会话」很容易被用户忽略 —— 因为它们和会话条目
 * 太像了：
 *
 *   - 「查看全部会话」直接复用了 `.side-nav-item`：同样 32px 高、同样
 *     左右 8px 内边距、同样是 muted 灰、同样圆角 8px，唯一区别只是更灰一点。
 *     它夹在一列同样高的会话条目下面，视觉上就是「又一条会话」。
 *   - 「对话和任务」是 13px 的灰色小字，和会话条目的 14px 正文只差 1px，
 *     没有分组/标题应有的层级差。
 *
 * ## 判定标准（可静态验证的契约）
 *
 * 1. 两者都不能只靠「稍微更灰」区分 —— 必须改变**形状或底色**（边框/填充/
 *    圆角/高度/字重），让它们脱离「列表行」的形态。
 * 2. 「查看全部会话」不能再复用 `.side-nav-item` 的视觉（不能只是加个类继续
 *    套用同样的盒子）。
 * 3. 「对话和任务」要有标题层级：字重或大小写/字距与会话正文拉开差距。
 *
 * 这里断言的是 CSS 契约（本仓库前端测试是 `node --test` 跑纯 TS，没有渲染栈）。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..')
const SIDEBAR = readFileSync(join(ROOT, 'src/components/Sidebar.tsx'), 'utf8')
const CSS = readFileSync(join(ROOT, 'src/styles/index.css'), 'utf8')

/** 取最后一个同名选择器的声明块（后写的覆盖前面的，取生效的那份）。 */
function cssBlock(selector: string): string {
  let from = 0
  let found = -1
  while (true) {
    const i = CSS.indexOf(selector + ' {', from)
    if (i < 0) break
    found = i
    from = i + 1
  }
  assert.ok(found >= 0, `CSS 里没有 ${selector} 的规则`)
  const end = CSS.indexOf('}', found)
  return CSS.slice(found, end)
}

/** 取某个选择器**所有**声明块的并集（多处声明会合并生效）。 */
function cssAll(selector: string): string {
  const parts: string[] = []
  let from = 0
  while (true) {
    const i = CSS.indexOf(selector + ' {', from)
    if (i < 0) break
    parts.push(CSS.slice(i, CSS.indexOf('}', i)))
    from = i + 1
  }
  assert.ok(parts.length > 0, `CSS 里没有 ${selector} 的规则`)
  return parts.join('\n')
}

const DECL = (block: string, prop: string): string | null => {
  const m = block.match(new RegExp(`(?:^|[;{\\s])${prop}:\\s*([^;}]+)`))
  return m ? m[1].trim() : null
}

// ---------------------------------------------------------------- 查看全部会话

test('「查看全部会话」有独立的类，不复用 side-nav-item 的视觉', () => {
  assert.match(SIDEBAR, /className="[^"]*\ball-sessions\b/,
    '找不到 all-sessions 类')
  // 仍然允许 side-nav-item 提供基础布局，但必须另有 all-sessions 覆写视觉
  assert.ok(CSS.includes('.all-sessions'), 'CSS 没有 .all-sessions 规则')
})

test('「查看全部会话」有可见的边框或底色（脱离"列表行"形态）', () => {
  const block = cssAll('.all-sessions')
  // `transparent` / `none` 不算视觉区分 —— 那等于没有。
  const visible = (v: string | null): boolean =>
    v !== null && !/^(transparent|none|0)\b/i.test(v.trim())

  const border =
    DECL(block, 'border')
    ?? [DECL(block, 'border-width'), DECL(block, 'border-top')].filter(Boolean).join(' ')
  const bg = DECL(block, 'background') ?? DECL(block, 'background-color')

  const hasBorder = visible(border) && !/^1px\s+none/i.test((border || '').trim())
  const hasFill = visible(bg)
  assert.ok(hasBorder || hasFill,
    `「查看全部会话」既没有可见边框也没有底色，和会话条目太像：\n${block}`)
})

test('「查看全部会话」不能用 muted 灰当主文字色', () => {
  const block = cssAll('.all-sessions')
  const color = DECL(block, 'color')
  assert.ok(color, '「查看全部会话」没有设定文字色（会继承 muted，太弱）')
  assert.ok(!/var\(--muted\)/.test(color),
    `「查看全部会话」文字色仍是 muted：${color}`)
})

test('「查看全部会话」有强调图标槽位（视觉抓手）', () => {
  const i = SIDEBAR.indexOf('all-sessions')
  const seg = SIDEBAR.slice(Math.max(0, i - 260), i + 320)
  assert.match(seg, /nav-ico|all-ico/,
    '「查看全部会话」没有图标，纯文字更难被注意到')
})

// ---------------------------------------------------------------- 对话和任务

test('「对话和任务」是小标题层级，不是正文大小', () => {
  const block = cssAll('.side-section')
  const size = DECL(block, 'font-size')
  assert.ok(size, '.side-section 没有 font-size')
  const px = parseFloat(size!)
  assert.ok(px <= 12.5,
    `.side-section 字号 ${size} 与会话条目 14px 太接近，撑不起标题层级`)
})

test('「对话和任务」有字重或字距强调', () => {
  const block = cssAll('.side-section')
  const weight = DECL(block, 'font-weight')
  const spacing = DECL(block, 'letter-spacing')
  const transform = DECL(block, 'text-transform')
  const ok = (weight && parseInt(weight, 10) >= 600) || spacing || transform
  assert.ok(ok,
    `.side-section 没有字重/字距/大小写强调，和普通灰色文字没区别：\n${block}`)
})

// ---------------------------------------------------------------- 结构性区分

test('两个锚点的盒子与会话条目不同（高度或内边距有差异）', () => {
  const item = cssAll('.session-item')
  const itemH = DECL(item, 'height')
  const all = cssAll('.all-sessions')
  const allH = DECL(all, 'height')
  assert.ok(itemH && allH, '缺少 height 定义，无法比较')
  assert.notEqual(allH, itemH,
    `「查看全部会话」高度 ${allH} 与会话条目 ${itemH} 相同 —— 会被当成普通列表行`)
})