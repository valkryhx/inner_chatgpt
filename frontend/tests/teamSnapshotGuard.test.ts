/**
 * Agent Team 侧栏卡在「正在启动...」的根因与修复（2026-11-19 用户报告）。
 *
 * ## 现象
 *
 * 新会话里让模型建 Agent Team（3 个 sub），右侧栏一直显示「[正在启动...]」，
 * 直到整个 team 任务跑完才显示真实任务板。
 *
 * ## 后端是正常的（实测排除）
 *
 * 抓真实 SSE 流（work/probe_team_timing.py）：
 *
 * ```
 * [  0.11s] team_event  type=team_start        <-- 很早就到前端了
 * [  3.99s] team_event  type=task_created
 * ...
 * [ 76.41s] done
 * ```
 *
 * 数据库里 team_events 也是 `team_start` 在 seq=1、时间戳最早。
 * 所以不是后端延迟、不是事件丢失。
 *
 * ## 根因：新会话里 loadTeam 的迟到响应覆盖
 *
 * `send()` 建会话后 `setActiveId(sid)`，触发
 * `useEffect([user, activeId, loadTeam])` → `loadTeam(sid)` →
 * `GET /api/team?session_id=sid`。
 *
 * 这个请求发出时后端**还没建 team**（team 在 `prepare_turn_team` 里建，
 * 晚于 HTTP 请求受理），所以返回 `{team: null, ...}` → `setTeamSnapshot(null)`。
 *
 * 等它返回时 `team_start` 可能已经把 snapshot 填好了 —— 这个 null 把它盖回去。
 * 之后没有任何代码再触发 `loadTeam`，于是 snapshot 永远 `team: null`，
 * 而 `starting = streaming && !team` 恒为真 → 侧栏卡在「正在启动...」，
 * 直到本轮结束 `streaming` 变 false 才显示。
 *
 * ## 这组测试钉什么
 *
 * 1. 流式期间的空快照**不能**覆盖已建立的 team（核心回归）
 * 2. 旧会话的迟到响应不能污染新会话
 * 3. 不能过度修复：历史快照、空对空、无 team 时都要正常应用
 * 4. App.tsx 真的调用了这个守卫（不是写了没人用的模块）
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import { shouldApplyTeamSnapshot } from '../src/teamSnapshotGuard.ts'

const here = dirname(fileURLToPath(import.meta.url))
const appSrc = readFileSync(join(here, '..', 'src', 'App.tsx'), 'utf8')

const snap = (teamId = null) => ({
  team: teamId ? { team_id: teamId, status: 'active' } : null,
  tasks: [],
  events: [],
  messages: [],
})

// ---------------- 核心回归 ----------------

test('流式期间的空快照不能覆盖已建立的 team', () => {
  // 这正是用户看到的场景：team_start 已经把 team 填好，
  // 然后 loadTeam 的迟到响应（team: null）回来
  const apply = shouldApplyTeamSnapshot({
    incoming: snap(null),
    current: snap('team_live'),
    incomingSession: 's1',
    currentSession: 's1',
    streaming: true,
  })
  assert.equal(apply, false, '空快照覆盖了刚建立的 team -> 侧栏会卡在「正在启动...」')
})

test('旧会话的迟到响应不能污染新会话', () => {
  const apply = shouldApplyTeamSnapshot({
    incoming: snap('team_old'),
    current: snap('team_new'),
    incomingSession: 's_old',
    currentSession: 's_new',
    streaming: false,
  })
  assert.equal(apply, false)
})

// ---------------- 不能过度修复 ----------------

test('没有 team 时，历史快照必须能正常加载', () => {
  // 刷新页面恢复任务板走的就是这条路径
  const apply = shouldApplyTeamSnapshot({
    incoming: snap('team_hist'),
    current: snap(null),
    incomingSession: 's1',
    currentSession: 's1',
    streaming: false,
  })
  assert.equal(apply, true)
})

test('空对空也要应用，否则切会话后残留上一轮的板', () => {
  const apply = shouldApplyTeamSnapshot({
    incoming: snap(null),
    current: snap(null),
    incomingSession: 's1',
    currentSession: 's1',
    streaming: false,
  })
  assert.equal(apply, true)
})

test('流式但当前还没有 team 时，空快照照常应用', () => {
  const apply = shouldApplyTeamSnapshot({
    incoming: snap(null),
    current: null,
    incomingSession: 's1',
    currentSession: 's1',
    streaming: true,
  })
  assert.equal(apply, true)
})

test('流式期间拿到有 team 的快照仍然要应用（任务可能更新了）', () => {
  const apply = shouldApplyTeamSnapshot({
    incoming: snap('team_live'),
    current: snap('team_live'),
    incomingSession: 's1',
    currentSession: 's1',
    streaming: true,
  })
  assert.equal(apply, true)
})

test('非流式时，空快照可以覆盖（用户切到真的没 team 的会话）', () => {
  const apply = shouldApplyTeamSnapshot({
    incoming: snap(null),
    current: snap('team_prev'),
    incomingSession: 's1',
    currentSession: 's1',
    streaming: false,
  })
  assert.equal(apply, true)
})

// ---------------- 接线检查 ----------------

test('App.tsx 的 loadTeam 真的调用了守卫', () => {
  assert.ok(
    appSrc.includes('shouldApplyTeamSnapshot'),
    'App.tsx 没有使用 shouldApplyTeamSnapshot —— 守卫没接上，bug 还在',
  )
})

test('App.tsx 传入了 streaming 状态给守卫', () => {
  // 守卫的核心判据是 streaming，漏传会让它退化成「永远应用」。
  //
  // ⚠️ 必须找**调用点**而不是 import 行：`indexOf` 会先命中
  // `import { shouldApplyTeamSnapshot } from ...`，那附近当然没有 streaming。
  // 因此从 `shouldApplyTeamSnapshot({` 开始找。
  const idx = appSrc.indexOf('shouldApplyTeamSnapshot({')
  assert.ok(idx > 0, '没找到守卫的调用点（是不是只 import 了没用？）')
  const vicinity = appSrc.slice(idx, idx + 600)
  assert.match(
    vicinity,
    /streaming/,
    '调用点没有传 streaming，守卫无法区分「流式中的迟到空快照」和「正常的空会话」',
  )
})