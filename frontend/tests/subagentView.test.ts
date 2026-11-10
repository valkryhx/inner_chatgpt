import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  capList,
  elapsedMs,
  formatDuration,
  isTerminal,
  lastToolError,
  mergeStart,
  mergeStatus,
} from '../src/subagentView.ts'

test('同一 agent 重复收到 start 不会多出一行', () => {
  const b = mergeStart([], { agent_id: 'a1', name: 'w', role: 'general', task: 't' })
  const b2 = mergeStart(b, { agent_id: 'a1', name: 'w', role: 'general', task: 't' })
  assert.equal(b2.length, 1)
})

test('不同 agent 各自一行', () => {
  let b = mergeStart([], { agent_id: 'a1', name: 'w1' })
  b = mergeStart(b, { agent_id: 'a2', name: 'w2' })
  assert.equal(b.length, 2)
})

test('终态不会被 running 覆盖', () => {
  assert.equal(mergeStatus('completed', 'running'), 'completed')
  assert.equal(mergeStatus('errored', 'running'), 'errored')
  assert.equal(mergeStatus('stopped', 'stopping'), 'stopped')
})

test('running 可以进入 stopping 和终态', () => {
  assert.equal(mergeStatus('running', 'stopping'), 'stopping')
  assert.equal(mergeStatus('running', 'completed'), 'completed')
  assert.equal(mergeStatus('stopping', 'stopped'), 'stopped')
})

test('stopping 不是终态', () => {
  assert.equal(isTerminal('stopping'), false)
  assert.equal(isTerminal('completed'), true)
  assert.equal(isTerminal('running'), false)
})

test('耗时格式：秒 / 分秒', () => {
  assert.equal(formatDuration(0), '0s')
  assert.equal(formatDuration(42_000), '42s')
  assert.equal(formatDuration(83_000), '1:23')
  assert.equal(formatDuration(3_725_000), '1:02:05')
})

test('运行中按 now 算耗时，结束后按 endedAt 算且不再增长', () => {
  const running = { startedAt: 1000, endedAt: undefined }
  assert.equal(elapsedMs(running, 5000), 4000)
  const done = { startedAt: 1000, endedAt: 7000 }
  assert.equal(elapsedMs(done, 99_999), 6000)
})

test('缺 startedAt 时返回 null，不伪造耗时', () => {
  assert.equal(elapsedMs({}, 5000), null)
})

test('失败原因优先取最后一条工具错误', () => {
  const block = {
    error: '任务超时',
    events: [
      { kind: 'tool_result', name: 'code_run', result: 'ok' },
      { kind: 'tool_result', name: 'file_read', result: 'no such file', is_error: true },
    ],
  }
  const r = lastToolError(block)
  assert.equal(r.source, 'tool')
  assert.equal(r.name, 'file_read')
  assert.match(r.text, /no such file/)
})

test('没有工具错误时回落到 block.error', () => {
  const r = lastToolError({ error: '任务超时', events: [] })
  assert.equal(r.source, 'error')
  assert.equal(r.text, '任务超时')
})

test('两者都没有返回 null', () => {
  assert.equal(lastToolError({ events: [] }), null)
})

test('capList 超出时返回隐藏数，未超出不隐藏', () => {
  const a = capList([1, 2, 3], 2)
  assert.deepEqual(a.items, [1, 2])
  assert.equal(a.hidden, 1)
  const b = capList([1], 2)
  assert.deepEqual(b.items, [1])
  assert.equal(b.hidden, 0)
})
