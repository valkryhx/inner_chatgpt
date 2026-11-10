/**
 * 思考等级（reasoning_effort）的候选值。
 *
 * 故意列全：各家 provider 支持的子集不一样（有的只认 low/medium/high，
 * 有的认得 minimal，Anthropic 系有 max，部分新端点才有 xhigh/ultra）。
 * 前端不做「这个模型支持哪些」的推断——那是猜；真不匹配时由后端透出
 * 「模型 X 不接受 reasoning_effort=Y」的明确错误。
 */

export const REASONING_LEVELS = [
  'none',
  'minimal',
  'low',
  'high',
  'xhigh',
  'max',
  'ultra',
] as const

export type ReasoningLevel = (typeof REASONING_LEVELS)[number]

/** 空字符串是合法值：表示不覆盖，沿用模型配置/上游默认。 */
export function isKnownLevel(value: string): boolean {
  if (value === '') return true
  return (REASONING_LEVELS as readonly string[]).includes(value)
}

export function effortLabel(value: string): string {
  const v = (value || '').trim().toLowerCase()
  if (!v) return '不指定'
  return v
}

export interface EffortOption {
  value: string
  label: string
}

/** 菜单项：第一项是「不指定」（清除覆盖），其余按固定顺序。 */
export function menuOptions(): EffortOption[] {
  return [
    { value: '', label: '不指定' },
    ...REASONING_LEVELS.map((lv) => ({ value: lv, label: lv })),
  ]
}
