import { test } from 'node:test'
import assert from 'node:assert/strict'

import { toolState } from '../src/toolState.ts'

test('unresolved tool on a finished message is stopped, not running', () => {
  assert.equal(toolState({ result: undefined }, true), 'stopped')
})

test('unresolved tool during streaming still runs', () => {
  assert.equal(toolState({ result: undefined }, false), 'running')
})

test('completed tool stays done even if the message ended', () => {
  assert.equal(toolState({ result: 'ok' }, true), 'done')
})

test('error result stays failed even if the message ended', () => {
  assert.equal(toolState({ result: '[工具执行错误] boom' }, true), 'failed')
})
