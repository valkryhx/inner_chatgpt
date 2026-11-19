/**
 * `done` 帧的 status 映射（2026-11-19 多实例真机发现）。
 *
 * ## 现场
 *
 * 3 实例 compose 环境里，对一个「消息是 streaming 但没有任何 active generation」
 * 的会话发续看请求，响应是：
 *
 * ```
 * event: done
 * data: {"message_id": "m_orphan", "status": "streaming", "model": ""}
 * ```
 *
 * 前端原映射 `ev.status === 'stopped' ? 'stopped' : ev.status === 'error' ? 'error' : 'complete'`
 * 会把 `'streaming'` 落到 else -> **`'complete'`**。
 * 于是「上一轮中断了」显示成「正常完成」，用户看到内容停在半截且没有任何提示。
 *
 * ## 这组测试钉什么
 *
 * 1. 三种真终态原样透传
 * 2. `'streaming'` **不能**变成 complete（核心回归）
 * 3. 未知值 / 空值不能变成 complete（兜底同一原则）
 * 4. App.tsx 真的用了这个函数（不是写了没人用的模块）
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import { doneStatus } from '../src/doneStatus.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..')
const APP = readFileSync(join(ROOT, 'src/App.tsx'), 'utf8')

// ---------------------------------------------------------------- 终态透传

test('complete 原样透传', () => {
  assert.equal(doneStatus('complete'), 'complete')
})

test('stopped 原样透传', () => {
  assert.equal(doneStatus('stopped'), 'stopped')
})

test('error 原样透传', () => {
  assert.equal(doneStatus('error'), 'error')
})

// ---------------------------------------------------------------- 核心回归

test('streaming 绝不能变成 complete', () => {
  assert.notEqual(doneStatus('streaming'), 'complete',
    '收到 done 但状态是 streaming = 这一轮没走完，不能谎报「已完成」')
  assert.equal(doneStatus('streaming'), 'stopped')
})

test('空值 / 未知值也不会变成 complete', () => {
  for (const raw of ['', null, undefined, 'weird', 'ACTIVE']) {
    const got = doneStatus(raw as string)
    assert.notEqual(got, 'complete', `未知状态 ${String(raw)} 被当成了完成`)
    assert.equal(got, 'stopped')
  }
})

// ---------------------------------------------------------------- 接线

test('App.tsx 用 doneStatus 映射 done 帧（两处都要）', () => {
  const hits = (APP.match(/doneStatus\(/g) || []).length
  assert.ok(hits >= 2,
    `App.tsx 里 doneStatus 只出现 ${hits} 次 —— 续看和主流程两处都要用`)

  // 旧的三元表达式不能还在（它正是 bug 来源）
  const legacy = "ev.status === 'stopped' ? 'stopped' : ev.status === 'error' ? 'error' : 'complete'"
  assert.ok(!APP.includes(legacy),
    'App.tsx 里还留着旧的三元映射 —— 它会把 streaming 映射成 complete')
})

test('后端确实会发出非终态 status（这个 bug 的源头仍在）', () => {
  // 只要后端仍可能把 DB 里的 streaming 原样传出，前端就必须兜住。
  // 这条断言是「提醒」性质：如果哪天后端改成只发终态，这里会红，
  // 提示可以重新评估 doneStatus 是否还需要兜底。
  const chat = readFileSync(
    join(HERE, '..', '..', 'backend/app/routers/chat.py'), 'utf8')
  assert.ok(chat.includes('last.get("status", "complete")'),
    '后端不再原样透传 status 了？如果是刻意改的，请更新 doneStatus 的注释')
})