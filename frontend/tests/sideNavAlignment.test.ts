/**
 * 侧边栏导航的视觉对齐契约。
 *
 * ## 问题（用户报告）
 *
 * 侧边栏那一列（新建 / 搜索 / 项目 / Artifacts / Code / 自定义）的**文字
 * 起点没有对齐成一条竖线**。
 *
 * 根因：`side-nav-item` 用 `display:flex; gap:10px`，图标是一个**无固定宽度**
 * 的 `<span>`。而这些图标是字形不是图标字体，占用宽度各不相同：
 *
 *   ＋  (全角 U+FF0B)      宽
 *   ⌕  (U+2315)            中
 *   ▤  (U+25A4)            中
 *   ◫  (U+25EB)            中
 *   ‹› (两个字符)          宽
 *   ⚙  (U+2699)            中
 *
 * `gap` 只保证「前一个元素的盒子边缘」等距，不保证文字起点对齐 ——
 * 盒子宽度不同，文字起点自然就参差。
 *
 * ## 修法
 *
 * 给图标一个**固定宽度的槽位**（`.side-nav-item > .nav-ico`），槽内居中，
 * 这样无论字形多宽，文字都从同一个 x 开始。
 *
 * ## 为什么用静态断言而不是渲染测试
 *
 * 本仓库的前端测试是 `node --test` 跑纯 TS，没有 JSX/浏览器渲染基建。
 * 为这一条对齐加一整套渲染栈不划算。所以断言两件可静态验证的事：
 *   1. 每个菜单项的图标都带 `nav-ico` 类（结构契约）
 *   2. CSS 里 `.nav-ico` 有固定 `width` 且 `flex: none`（布局契约）
 * 这两条同时成立，文字起点就必然对齐。
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

/** 抽出 `.side-nav` 里的全部按钮（这一段就是用户说的那一列）。 */
function navItems(): string[] {
  const start = SIDEBAR.indexOf('<nav className="side-nav">')
  const end = SIDEBAR.indexOf('</nav>', start)
  assert.ok(start >= 0 && end > start, '找不到 <nav className="side-nav"> 区块')
  const block = SIDEBAR.slice(start, end)
  // 每个菜单项是一个 <button ...> ... </button>
  return block.split('<button').slice(1)
}

/** 抽出 CSS 里某个选择器的声明块（取第一次出现）。 */
function cssBlock(selector: string): string {
  const i = CSS.indexOf(selector + ' {')
  assert.ok(i >= 0, `CSS 里没有 ${selector} 的规则`)
  const end = CSS.indexOf('}', i)
  return CSS.slice(i, end)
}

test('侧边栏这一列至少有 6 个菜单项（防止选择器写错后空跑）', () => {
  const items = navItems()
  assert.ok(items.length >= 6, `只找到 ${items.length} 个菜单项`)
})

test('每个菜单项的图标都带 nav-ico 类', () => {
  const items = navItems()
  const bad: string[] = []
  for (const item of items) {
    // 图标是按钮里的第一个 <span ...>（属性顺序不固定，不能假设 aria-hidden 在最前）
    const m = item.match(/<span\b[^>]*>([\s\S]*?)<\/span>/)
    if (!m) {
      bad.push(`<button${item.slice(0, 40)}… 没找到图标 span`)
      continue
    }
    const openTag = m[0].slice(0, m[0].indexOf('>') + 1)
    if (!/className="[^"]*\bnav-ico\b/.test(openTag)) {
      bad.push(`图标 ${m[1].trim()!} 缺 nav-ico 类：${openTag}`)
    }
  }
  assert.deepEqual(bad, [], `这些菜单项的图标没有固定槽位：\n${bad.join('\n')}`)
})

test('nav-ico 有固定宽度且不参与伸缩', () => {
  const block = cssBlock('.side-nav-item .nav-ico')
  assert.match(block, /(?:^|[;{\s])width:\s*\d+(?:\.\d+)?px/, 'nav-ico 没有固定 width')
  assert.match(block, /flex:\s*none|flex-shrink:\s*0/, 'nav-ico 会被 flex 压缩，宽度不固定')
})

test('nav-ico 水平居中（不同宽度的字形都居中对齐）', () => {
  const block = cssBlock('.side-nav-item .nav-ico')
  assert.match(block, /justify-content:\s*center/, 'nav-ico 内容没有居中')
  assert.match(block, /display:\s*(inline-)?flex/, 'nav-ico 不是 flex 容器')
})

test('nav-ico 不继承正文的 letter-spacing（双字符图标 ‹› 会偏）', () => {
  const block = cssBlock('.side-nav-item .nav-ico')
  assert.match(block, /letter-spacing:\s*0/, 'nav-ico 没有清掉 letter-spacing')
})