/**
 * 「连错实例」的自动重试契约（2026-11-19 审计 §4 前端配套）。
 *
 * ## 这组测试钉什么
 *
 * 1. `resumeRetryAction` 只允许重试**一次**（防无限循环）
 * 2. `isWrongInstanceError` 认得后端那个固定 token
 * 3. **两端的字面量必须一致** —— 后端 `chat.py` 写 `"wrong_instance"`，
 *    前端 `resumeRetry.ts` 匹配同一个串。任何一边改了另一边没改，
 *    这条测试就红。这是最容易悄悄失配的地方。
 * 4. `App.tsx` 真的接上了（不是写了个没人用的模块）
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import { resumeRetryAction, isWrongInstanceError, planResumeRetry } from '../src/resumeRetry.ts'

const HERE = dirname(fileURLToPath(import.meta.url))   // frontend/tests
const ROOT = join(HERE, '..')                          // frontend
const APP = readFileSync(join(ROOT, 'src/App.tsx'), 'utf8')
const RETRY = readFileSync(join(ROOT, 'src/resumeRetry.ts'), 'utf8')
// 后端源码：跨仓库读。tests -> frontend -> inner_chatgpt -> backend
// （`HERE` 已经是 tests，所以是三级上跳，第一版写成两级，路径落到了
//  D:\git_codes\backend 上，测试直接 ENOENT 报出来）
const BACKEND_CHAT = join(HERE, '..', '..', 'backend', 'app', 'routers', 'chat.py')

// ---------------------------------------------------------------- 重试次数

test('第一次遇到 wrong_instance 要重试', () => {
  assert.equal(resumeRetryAction(false), 'rehandshake')
})

test('已经重试过就放弃（防无限循环）', () => {
  assert.equal(resumeRetryAction(true), 'give-up')
})

// ---------------------------------------------------------------- token 识别

test('认得后端返回的 wrong_instance', () => {
  assert.equal(isWrongInstanceError('wrong_instance'), true)
})

test('认得拼接过的错误文本（streamErrorBody 会 join）', () => {
  assert.equal(
    isWrongInstanceError('wrong_instance：这一轮在另一个实例上运行。'),
    true,
  )
})

test('普通错误不误判', () => {
  for (const text of [
    'stream failed (500)',
    'too_many_subscribers',
    'generation_active',
    '这一轮在另一个实例上运行',   // 只有中文、没有 token
    '',
    null,
    undefined,
  ]) {
    assert.equal(isWrongInstanceError(text as string), false, `误判了：${text}`)
  }
})

// ---------------------------------------------------------------- 两端一致

test('后端确实返回 wrong_instance 这个 token', () => {
  const src = readFileSync(BACKEND_CHAT, 'utf8')
  assert.match(src, /"error":\s*"wrong_instance"/,
    'chat.py 里找不到 "error": "wrong_instance" —— 后端改了 token 而前端没跟上')
})

test('前端匹配的 token 与后端字面量相同', () => {
  const backendSrc = readFileSync(BACKEND_CHAT, 'utf8')
  const m = backendSrc.match(/"error":\s*"(wrong_instance)"/)
  assert.ok(m, '后端 token 提取失败')
  const backendToken = m![1]

  // 前端那一行必须是同一个字面量
  const fm = RETRY.match(/includes\('([a-z_]+)'\)/)
  assert.ok(fm, 'resumeRetry.ts 里找不到 includes(...) 的 token')
  assert.equal(fm![1], backendToken,
    `前端匹配 '${fm![1]}' 而后端返回 '${backendToken}' —— 两边失配，重试永远不触发`)
})

// ---------------------------------------------------------------- 真的接上了

test('resumeRetry 不是孤儿模块（App.tsx 引用了它）', () => {
  assert.match(APP, /from '\.\/resumeRetry'/,
    'App.tsx 没有 import resumeRetry —— 写了但没接线，等于没修')
})

test('App.tsx 里有「重新握手」的动作（先打 health 换 cookie）', () => {
  assert.match(APP, /planResumeRetry/,
    'App.tsx 没用 planResumeRetry 判定')
  assert.match(APP, /api\.health\(\)/,
    'App.tsx 没有重新调 health —— 不换 cookie 的重试会落到同一个错实例，是无效动作')
})

// ---------------------------------------------------------------- 决策（行为级）

test('不是连错实例就不重试', () => {
  assert.equal(planResumeRetry(false, false), 'no-retry')
  assert.equal(planResumeRetry(false, true), 'no-retry')
})

test('连错实例且没重试过 -> 换 cookie 后重试', () => {
  assert.equal(planResumeRetry(true, false), 'rehandshake-then-retry')
})

test('连错实例但已重试过 -> 放弃（防无限循环）', () => {
  assert.equal(planResumeRetry(true, true), 'no-retry')
})