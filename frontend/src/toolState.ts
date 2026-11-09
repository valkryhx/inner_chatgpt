import type { ToolBlock } from './types'

export type ToolState = 'running' | 'failed' | 'done' | 'stopped'

/**
 * Resolve a tool step's visual state.
 *
 * A missing result means "still running" only while the parent message is
 * streaming. Once the turn has ended (stop, error, crash, or plain EOF), an
 * unresolved step can never finish, so it must render as interrupted instead
 * of a permanent spinner.
 */
export function toolState(block: Pick<ToolBlock, 'result'>, interrupted: boolean): ToolState {
  if (block.result === undefined) return interrupted ? 'stopped' : 'running'
  if (block.result.startsWith('[工具执行错误]')) return 'failed'
  return 'done'
}
