import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  REASONING_LEVELS,
  effortLabel,
  isKnownLevel,
  menuOptions,
} from '../src/reasoningLevels.ts'

test('七个档位 + 「不指定」都在', () => {
  for (const lv of ['none', 'minimal', 'low', 'high', 'xhigh', 'max', 'ultra']) {
    assert.ok(REASONING_LEVELS.includes(lv as never), lv)
    assert.ok(isKnownLevel(lv))
  }
})

test('空值表示不指定，不是非法值', () => {
  assert.equal(isKnownLevel(''), true)
  assert.equal(effortLabel(''), '不指定')
})

test('未知档位判非法（前端也要挡，不只是后端）', () => {
  assert.equal(isKnownLevel('turbo'), false)
})

test('菜单第一项是「不指定」，用于清除覆盖', () => {
  const opts = menuOptions()
  assert.equal(opts[0].value, '')
  assert.equal(opts.length, REASONING_LEVELS.length + 1)
})

test('档位标签可读', () => {
  assert.equal(effortLabel('xhigh'), 'xhigh')
  assert.equal(effortLabel('none'), 'none')
})
