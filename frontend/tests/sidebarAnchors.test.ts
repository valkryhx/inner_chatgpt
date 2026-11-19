/**
 * 侧边栏结构：「查看全部会话」属于导航区，会话列表标题在其下方。
 *
 * ## 为什么这样分组（用户 2026-11-19 的调整）
 *
 * 「查看全部会话」是一个**可点击的动作**，和上面的新建 / 搜索 / 自定义
 * 是同一类东西，所以它应该和它们放在一起、长得一样（同款图标槽位、
 * 同款文字色），而不是挂在会话列表末尾。
 *
 * 之前把它做成「带边框的异形按钮」是为了让它从会话条目里跳出来，
 * 但那治的是症状：真正的区分手段是**分组** —— 它在导航区里，
 * 自然不会和下面的会话条目混在一起。
 *
 * 所以现在的契约是：
 *   1. 它在 `<nav class="side-nav">` 内部，紧跟在「自定义」之后
 *   2. 它用 `nav-ico` 图标槽位（与其它导航项一致）
 *   3. 它的图标不是 clay 强调色（要「一致的黑色」= 继承导航项文字色）
 *   4. 「对话和任务」标题在它**下方**
 *
 * 顺带保留：「对话和任务」仍是小标题层级（12px + 600 字重），
 * 不能退回正文大小 —— 否则用户又会当普通文字跳过。
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

const NAV_OPEN = SIDEBAR.indexOf('<nav className="side-nav">')
const NAV_CLOSE = SIDEBAR.indexOf('</nav>', NAV_OPEN)
// 注意：`onOpenAllSessions` 在 props 类型声明和参数解构里也出现，
// 所以必须找**按钮**那一处，不能用 indexOf('onOpenAllSessions')。
const ALL_BTN = SIDEBAR.indexOf('<button className="side-nav-item" onClick={onOpenAllSessions}>')
const ALL_IDX = ALL_BTN >= 0 ? ALL_BTN : -1
const SECTION_IDX = SIDEBAR.indexOf('对话和任务')

/** 取某个选择器**所有**声明块的并集（后写的会覆盖前面的，合并看）。 */
function cssAll(selector: string): string {
  const parts: string[] = []
  let from = 0
  while (true) {
    const i = CSS.indexOf(selector + ' {', from)
    if (i < 0) break
    parts.push(CSS.slice(i, CSS.indexOf('}', i)))
    from = i + 1
  }
  return parts.join('\n')
}

const DECL = (block: string, prop: string): string | null => {
  const m = block.match(new RegExp(`(?:^|[;{\\s])${prop}:\\s*([^;}]+)`))
  return m ? m[1].trim() : null
}

// ---------------------------------------------------------------- 位置

test('能找到 nav 区块与两个关键元素（防止选择器写错后空跑）', () => {
  assert.ok(NAV_OPEN >= 0 && NAV_CLOSE > NAV_OPEN, '找不到 <nav className="side-nav">')
  assert.ok(ALL_IDX > 0, '找不到 onOpenAllSessions')
  assert.ok(SECTION_IDX > 0, '找不到「对话和任务」')
})

test('「查看全部会话」在导航区内（与新建/搜索/自定义同组）', () => {
  assert.ok(ALL_IDX > NAV_OPEN && ALL_IDX < NAV_CLOSE,
    '「查看全部会话」不在 <nav className="side-nav"> 里 —— 它应当和上面的按钮同组')
})

test('「查看全部会话」紧跟在「自定义」之后', () => {
  // 同样不能 indexOf('onOpenSettings')：props 类型里也有它。
  const settingsBtn = SIDEBAR.indexOf(
    '<button className="side-nav-item" onClick={onOpenSettings}>')
  assert.ok(settingsBtn > 0, '找不到「自定义」按钮')
  assert.ok(ALL_IDX > settingsBtn,
    '「查看全部会话」应在「自定义」之后，成为导航区的最后一项')
  // 两者之间不应再插入别的 <button
  const between = SIDEBAR.slice(settingsBtn, ALL_IDX)
  const extraButtons = (between.match(/<button/g) || []).length
  assert.equal(extraButtons, 1,
    `「自定义」和「查看全部会话」之间夹着 ${extraButtons - 1} 个额外按钮`)
})

test('「对话和任务」在「查看全部会话」下方', () => {
  assert.ok(SECTION_IDX > ALL_IDX,
    '「对话和任务」标题应在「查看全部会话」之后出现')
})

// ---------------------------------------------------------------- 图标一致

test('「查看全部会话」用 nav-ico 图标槽位（与其它导航项一致）', () => {
  const seg = SIDEBAR.slice(ALL_IDX, ALL_IDX + 400)
  assert.match(seg, /className="nav-ico"/,
    '「查看全部会话」的图标没有用 nav-ico —— 会和其它导航项的图标竖线不齐')
})

test('「查看全部会话」的图标不使用 clay 强调色（要一致的黑色）', () => {
  // 导航项文字色是 --ink，nav-ico 继承即可。任何给 all-ico/all-sessions
  // 单独指定 clay 的规则都会破坏「一致」。
  for (const sel of ['.all-ico', '.all-sessions .nav-ico', '.all-sessions .all-ico']) {
    const block = cssAll(sel)
    if (!block) continue
    assert.ok(!/var\(--clay\)/.test(block),
      `${sel} 仍把图标设成 clay 强调色：\n${block}`)
  }
  // 同时确认 JSX 里不再有专用的 all-ico 槽位
  const seg = SIDEBAR.slice(ALL_IDX, ALL_IDX + 400)
  assert.ok(!/className="all-ico"/.test(seg),
    '还有专用的 all-ico 槽位 —— 图标颜色/宽度会和导航项不一致')
})

test('「查看全部会话」不再是异形按钮（不带独立边框/底色）', () => {
  const block = cssAll('.all-sessions')
  if (!block) return  // 完全没有专用规则也满足「与导航项一致」
  const visible = (v: string | null): boolean =>
    v !== null && !/^(none|transparent|0)\b/i.test(v.trim())
  for (const prop of ['border', 'background', 'background-color']) {
    const v = DECL(block, prop)
    if (v === null) continue
    assert.ok(!visible(v),
      `${prop} 仍被单独设为可见值「${v}」—— 它应当和导航项长得一样`)
  }
})

// ---------------------------------------------------------------- 标题层级

test('「对话和任务」是小标题层级，不是正文大小', () => {
  const block = cssAll('.side-section')
  assert.ok(block, 'CSS 里没有 .side-section 规则')
  const size = DECL(block, 'font-size')
  assert.ok(size, '.side-section 没有 font-size')
  assert.ok(parseFloat(size!) <= 12.5,
    `.side-section 字号 ${size} 与会话条目太接近，撑不起标题层级`)
})

test('「对话和任务」有字重或字距强调', () => {
  const block = cssAll('.side-section')
  const weight = DECL(block, 'font-weight')
  const spacing = DECL(block, 'letter-spacing')
  const transform = DECL(block, 'text-transform')
  const ok = (weight && parseInt(weight, 10) >= 600) || spacing || transform
  assert.ok(ok, `.side-section 没有字重/字距/大小写强调：\n${block}`)
})