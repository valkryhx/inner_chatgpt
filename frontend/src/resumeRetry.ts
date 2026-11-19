/**
 * 续看收到「连错实例」时要自己重试一次（2026-11-19 审计 §4 的前端配套）。
 *
 * ## 背景
 *
 * 后端 `/sessions/<id>/stream` 现在会在「这一轮跑在**别的实例**上」时返回
 * `409 wrong_instance`（`chat.py:_resume_decision`）。修这个后端问题**只做一半**
 * 是不够的：如果前端把它当普通错误显示出来，用户体验从「内容停住」变成
 * 「看到一个红色报错」，本质没改善。
 *
 * 完整闭环需要：
 *
 * 1. 后端说清楚「你连错了」（已做）
 * 2. 前端**重新握手**（拿到新 cookie 落到正确实例）后重试一次
 * 3. 重试还不行，才把错误显示给用户
 *
 * 这个文件只管第 2 步的**判定逻辑**，抽成纯函数以便真跑测试 ——
 * 判定逻辑藏在 `App.tsx` 里就只能靠静态断言，那种断言测不到行为
 * （本仓库已踩过：静态断言在注释里出现关键词时会在错误的原因下通过）。
 *
 * ## 为什么要「重新握手」
 *
 * nginx 用 `hash $cookie_hlz_sticky` 做粘性（见 `backend/deploy/nginx.conf`）。
 * 落到错实例说明当前 cookie 与服务端哈希环不匹配（扩缩容后重算、
 * 或 cookie 被清）。单纯重试同一个请求还会落到同一个错实例 ——
 * **必须先把 cookie 换掉**，否则重试是无效动作。
 *
 * 而已有机制里，`/api/health` 是唯一会下发 sticky cookie 的端点
 * （`main.py:274`）。所以「重新握手」= 先打一次 health。
 */

/** 续看失败后应该做什么。 */
export type ResumeRetryAction =
  /** 连错实例：刷新粘性 cookie 后重试一次。 */
  | 'rehandshake'
  /** 其他错误 / 已经重试过：不要重试，直接报给用户。 */
  | 'give-up'

/**
 * 决定收到 `409 wrong_instance` 之后该怎么做。
 *
 * @param alreadyRetried 本次续看是否已经重试过 —— 只重试一次，
 *   否则「两个实例互相认为对方是 owner」这类配置错误会变成无限循环。
 */
export function resumeRetryAction(alreadyRetried: boolean): ResumeRetryAction {
  return alreadyRetried ? 'give-up' : 'rehandshake'
}

/**
 * 从一条 SSE error 帧 / HTTP 错误体里判断是不是「连错实例」。
 *
 * ## 为什么要做字符串匹配而不是状态码
 *
 * `resumeStream` 目前把非 2xx 的响应体压成一行错误文本再走
 * `onEvent({type:'error'})`（`api.ts:479 streamErrorBody`），
 * 状态码在这一层已经丢了。要改成保留状态码会牵动 `StreamEvent` 类型
 * 和所有消费方 —— 为了一个分支不值得。
 *
 * 所以这里退一步做文本匹配，但**匹配的是我们自己的固定 token**
 * （后端 `content={"error": "wrong_instance", ...}`），不是任何用户文本。
 * 后端改动时这个 token 必须一起改，`tests/wrongInstanceRetry.test.ts`
 * 断言了两端的字面量一致。
 */
export function isWrongInstanceError(text: string | null | undefined): boolean {
  if (!text) return false
  return text.includes('wrong_instance')
}

/**
 * 续看失败后的**完整决策**：要不要重试、重试前要不要重新握手。
 *
 * ## 为什么要抽成函数而不是留在 App.tsx 里
 *
 * 第一版把这段逻辑写在 `App.tsx` 的 `resumeIfRunning` 里，测试只能做静态断言
 * （「源码里出现了 resumeRetryAction」）。结果是**反向确认没变红** ——
 * 把真正的调用改成 `if (false && ...)`，静态断言照样匹配。那种测试测不到行为。
 *
 * 抽出来之后 `tests/wrongInstanceRetry.test.ts` 直接调它，注入各种组合，
 * 反向确认（把分支改成恒 false）立刻就红。
 *
 * ## 三种输入 -> 三种结果
 *
 * | wrongInstance | alreadyRetried | 结果 |
 * | --- | --- | --- |
 * | false | 任意 | `no-retry`（不是连错实例，不重试） |
 * | true | false | `rehandshake-then-retry`（换 cookie 后重试一次） |
 * | true | true | `no-retry`（已经重试过，不再循环） |
 */
export type ResumePlan = 'no-retry' | 'rehandshake-then-retry'

export function planResumeRetry(
  wrongInstance: boolean,
  alreadyRetried: boolean,
): ResumePlan {
  if (!wrongInstance) return 'no-retry'
  if (resumeRetryAction(alreadyRetried) === 'give-up') return 'no-retry'
  return 'rehandshake-then-retry'
}