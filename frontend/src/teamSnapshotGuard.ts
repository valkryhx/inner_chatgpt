/**
 * Agent Team snapshot 的写入守卫。
 *
 * ## 为什么需要（2026-11-19 用户报告）
 *
 * 新会话里让模型建 Agent Team，右侧栏一直卡在「[正在启动...]」，
 * 直到整个 team 任务跑完才显示真实任务板。
 *
 * 后端是正常的 —— 抓真实 SSE 流，`team_start` 在 **0.11 秒**就到达前端。
 * 问题在 `send()` 里的这段顺序：
 *
 * ```ts
 * const created = await api.createSession(...)
 * sid = created.id
 * setActiveId(sid)      // 触发 useEffect -> loadTeam(sid)
 * ```
 *
 * `loadTeam` 打的是 `GET /api/team?session_id=sid`，而这个请求发出时
 * 后端**还没建 team**（team 在 `prepare_turn_team` 里建，晚于 HTTP 请求受理），
 * 于是返回 `{team: null, ...}` → `setTeamSnapshot(null)`。
 *
 * 等这个响应回来时，`team_start` 可能已经把 snapshot 填好了 —— 这个 null
 * 又把它盖回去。之后没有任何代码再触发 `loadTeam`，于是 snapshot 永远是
 * `team: null`，而 `starting = streaming && !team` 恒为真 → 侧栏一直「正在启动...」，
 * 直到本轮结束 `streaming` 变 false 才显示。
 *
 * ## 判据
 *
 * 只有「**流式进行中** + 到来的快照没有 team + 当前已经有 team」这一种组合
 * 是危险的，其余一律照常应用。这样：
 *
 * - 刷新页面恢复任务板：`current` 为空 → 照常应用 ✅
 * - 用户主动切到没有 team 的会话：非流式 → 照常应用 ✅
 * - 流式中任务更新（带 team 的快照）：照常应用 ✅
 */

export interface TeamSnapshotLike {
  team: unknown | null
  tasks: unknown[]
  events: unknown[]
  messages: unknown[]
}

export interface ApplyDecision {
  /** 迟到的历史快照（可能没有 team） */
  incoming: TeamSnapshotLike | null
  /** 当前面板上的快照 */
  current: TeamSnapshotLike | null
  /** 发起这次请求时的会话 id */
  incomingSession: string | null
  /** 请求返回时的当前会话 id */
  currentSession: string | null
  /** 请求返回时是否仍在生成 */
  streaming: boolean
}

/**
 * 迟到的历史快照该不该覆盖当前 snapshot。
 *
 * 规则按顺序：
 * 1. 会话对不上 -> 不应用（旧会话的响应，别污染新会话）
 * 2. 流式中 + 到来的是空 team + 当前已有 team -> 不应用（本 bug 的场景）
 * 3. 其余 -> 应用
 */
export function shouldApplyTeamSnapshot(d: ApplyDecision): boolean {
  if (d.incomingSession !== d.currentSession) return false

  const incomingHasTeam = Boolean(d.incoming && d.incoming.team)
  const currentHasTeam = Boolean(d.current && d.current.team)

  if (d.streaming && !incomingHasTeam && currentHasTeam) return false

  return true
}