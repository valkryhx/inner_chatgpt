/**
 * 缺 team_start 时，team 快照必须能从后续事件自举（2026-11-19 用户报告）。
 *
 * ## 现象
 *
 * 用户让 hlaudz 建 Agent Team（3 个 sub），右侧栏一直「正在启动...」，
 * 直到任务跑完才显示。
 *
 * ## 根因（实测证据）
 *
 * 页面里 hook fetch 抓到的真实帧序列：
 *
 * ```
 * +  10ms  message_start          [/api/sessions/../messages]
 * +1343ms  team_event:task_created
 * +3800ms  team_event:task_created
 * ...
 * 收到 team_start: False
 * ```
 *
 * 库里 seq=1 **确实是 team_start**，但前端没收到。原因在续看路径：
 * `GET /api/sessions/{id}/stream?after=0` 走 `turn_job.sync_frames(0)`，
 * 该函数在「after 太老」时只发一帧 `sync` 快照，**不重放历史 team_event**；
 * 而前端处理 `sync` 只替换 blocks，不重建 teamSnapshot。
 *
 * 于是前端永远拿不到 team_start。而 `mergeTeamEvent` 有这一行：
 *
 * ```ts
 * if (!prev || !prev.team) return prev    // 没有 team 对象，后续事件全部丢弃
 * ```
 *
 * 结果：task_created 全被丢掉 → team 恒为 null → `starting = streaming && !team`
 * 恒为真 → 侧栏卡在「正在启动...」，直到整轮结束 streaming 变 false。
 *
 * 这也解释了为什么同一个探针跑两次一次 PASS 一次 FAIL：走 POST 直连时
 * team_start 能到；走 resume/sync 时不到。
 *
 * ## 修复
 *
 * 允许从带 `team_id` 的业务事件**自举**出一个最小 team 对象，
 * 而不是把后续事件全丢掉。自举出来的状态是 `active`，等真正的
 * `team_end` 再更新。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { mergeTeamEvent } from '../src/teamMerge.ts'

const ev = (type, seq, extra = {}) => ({
  type,
  team_id: extra.team_id ?? 'team_x',
  session_id: 's1',
  generation_id: 'g1',
  agent_id: 'root',
  event_seq: seq,
  payload: extra.payload ?? {},
  updated_at: Date.now(),
})

const taskPayload = (id) => ({
  task: {
    task_id: id, team_id: 'team_x', user_id: 'u1', session_id: 's1',
    generation_id: 'g1', subject: 'step', description: '', status: 'pending',
    owner_agent_id: '', created_by: 'root', created_at: 1, updated_at: 1,
    version: 1, blocked_by: [], result: '',
  },
})

// ---------------- 核心回归 ----------------

test('没有 team_start 时，task_created 自举出 team 快照', () => {
  const out = mergeTeamEvent(null, ev('task_created', 2, { payload: taskPayload('t1') }))
  assert.ok(out, 'task_created 被整个丢掉了')
  assert.ok(out.team, '没有自举出 team 对象')
  assert.equal(out.team.team_id, 'team_x')
  assert.equal(out.tasks.length, 1, '任务没有被收进快照')
})

test('自举后，后续 task_updated 能正常合并', () => {
  const a = mergeTeamEvent(null, ev('task_created', 2, { payload: taskPayload('t1') }))
  const updated = {
    task: { ...taskPayload('t1').task, status: 'completed' },
  }
  const b = mergeTeamEvent(a, ev('task_updated', 3, { payload: updated }))
  assert.equal(b.tasks.length, 1)
  assert.equal(b.tasks[0].status, 'completed')
})

test('自举不会覆盖已有的 team', () => {
  const a = mergeTeamEvent(null, ev('team_start', 1, { payload: { name: 'My Team' } }))
  const b = mergeTeamEvent(a, ev('task_created', 2, { payload: taskPayload('t1') }))
  assert.equal(b.team.name, 'My Team', '自举覆盖了 team_start 建立的名字')
})

// ---------------- team_start 仍然优先 ----------------

test('team_start 仍能替换不同 team_id 的旧快照', () => {
  const old = mergeTeamEvent(null, ev('team_start', 1, { team_id: 'team_old' }))
  const newer = mergeTeamEvent(old, ev('team_start', 1, {
    team_id: 'team_new', payload: { name: 'New Team' },
  }))
  assert.equal(newer.team.team_id, 'team_new')
  assert.equal(newer.team.name, 'New Team')
  assert.equal(newer.tasks.length, 0, '新 team 应该清空旧任务')
})

// ---------------- 不能误伤 ----------------

test('别的 team 的事件仍然被忽略', () => {
  const a = mergeTeamEvent(null, ev('team_start', 1, { team_id: 'team_a' }))
  const b = mergeTeamEvent(a, ev('task_created', 5, {
    team_id: 'team_b', payload: taskPayload('t9'),
  }))
  assert.equal(b.team.team_id, 'team_a')
  assert.equal(b.tasks.length, 0, '别的 team 的任务不该混进来')
})

test('team_unavailable 在没有 team 时也要保留（用于提示原因）', () => {
  const out = mergeTeamEvent(null, {
    type: 'team_unavailable', team_id: '', session_id: 's1',
    generation_id: 'g1', agent_id: 'root', event_seq: 1,
    payload: { error: 'quota exceeded' }, updated_at: 1,
  })
  assert.ok(out.events.some((e) => e.type === 'team_unavailable'))
})

test('team_end 更新状态', () => {
  const a = mergeTeamEvent(null, ev('team_start', 1))
  const b = mergeTeamEvent(a, ev('team_end', 9, { payload: { status: 'completed' } }))
  assert.equal(b.team.status, 'completed')
})