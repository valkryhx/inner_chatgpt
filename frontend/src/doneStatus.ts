/**
 * `done` 帧的 status 映射（2026-11-19 多实例验证发现）。
 *
 * ## 问题
 *
 * 续看端点在「没有活 job」时会发一帧 `done`，`status` 取自 DB：
 *
 * ```python
 * yield sse_frame("done", {"status": last.get("status", "complete")})
 * ```
 *
 * 而 DB 里的 status 可能是 `'streaming'` —— 这表示**那一轮没有正常结束**
 * （进程崩溃 / 竞态 / 收敛还没跑到）。原前端映射是：
 *
 * ```ts
 * ev.status === 'stopped' ? 'stopped' : ev.status === 'error' ? 'error' : 'complete'
 * ```
 *
 * `'streaming'` 掉进 else -> **变成 `'complete'`**。于是「上一轮中断了」
 * 显示成「正常完成」，用户看到内容停在半截却没有任何提示。
 *
 * 这是审计 §4 记的那个「放大点」的另一半：后端修好了「不在别人跑着时发 done」，
 * 但**真的中断**时发的那个 done 仍带着 streaming，前端照样误判。
 *
 * ## 修法
 *
 * 把映射抽成纯函数（可测），并且：**只有明确的终态才映射成终态**，
 * 其余一律 `'stopped'` —— 因为「收到了 done 但状态不是终态」本身就意味着
 * 这一轮没走完，`stopped` 是诚实的表达（前端会显示“已停止”而不是“已完成”）。
 *
 * 为什么不改成 `'error'`：中断不是错误，用户不需要看到报错。`stopped`
 * 的 UI 语义是「这一轮没继续」，正是事实。
 */

export type MessageStatus = 'complete' | 'stopped' | 'error' | 'streaming'

/**
 * 把 `done` 帧里的 status 映射成前端要落库的消息状态。
 *
 * @param raw 服务端给的 status（可能是任何值，包括 `'streaming'` 这种非终态）
 */
export function doneStatus(raw: string | null | undefined): MessageStatus {
  if (raw === 'complete') return 'complete'
  if (raw === 'error') return 'error'
  if (raw === 'stopped') return 'stopped'
  // 'streaming' / 空 / 未知 —— 收到 done 却不是终态，说明这一轮没正常走完。
  // 落成 stopped 而不是 complete：显示「已停止」比谎报「已完成」诚实。
  return 'stopped'
}