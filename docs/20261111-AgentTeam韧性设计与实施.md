# Agent Team 韧性（Resilience）：API 中断自愈与团队不崩

> 日期：2026-11-11　状态：**设计与实施**
> 触发：真机跑多智能体链路时，lead 因一次 LLM API 中断僵死 927 秒，任务 3 永久停在 `pending`

## 1. 问题：一个 agent 的 API 中断，会让整个团队废掉

### 1.1 真机现场

`work/live/111915-64a6/share.json`（runId `111915-64a6`）：

| 观测 | 值 |
|---|---|
| 总耗时 | **927.9 秒** |
| assistant 消息状态 | **`error`** |
| 任务 3 | **`pending`（永久）** |
| 最终答案含哨兵 | **False** |
| digger / reader | 都 `completed`，各跑满自己的轮次 |

现场证据（持久化 blocks 的尾部）：lead 最后一次 `code_run` 返回了正常结果（`exit=0`，
确实读到了 `host.py` 第 8 行 `from __future__ import annotations`），**之后再没有任何
block** —— 无 text、无 tool、无 error block。也就是说：模型调用发出去了，然后什么都没回来。

`app.log` 里没有 traceback，说明不是未捕获异常，而是调用层在超时/断流后不重试、
循环直接结束。

### 1.2 根因（代码级）

`backend/app/llm_client.py:299-399`：

```python
attempt = 0
while True:
    got_any = False
    ...
    if retryable and attempt < self.s.max_retries and not got_any:   # L321
        attempt += 1; continue
    ...
    except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as e:
        if attempt < self.s.max_retries and not got_any:              # L396
            attempt += 1; continue
        raise LLMError(...)
```

`not got_any` 的语义是**「一旦流里出过任何 delta，就不再重试」**。这个规则本身有道理
（避免把已经吐给用户的内容重复吐一遍），但它有两个致命副作用：

1. **已经输出一点内容就断流 → 永不重试**。长回答、长思考的模型尤其容易撞上。
2. **重试预算被 `max_retries=2` 卡死**，且没有退避、没有 `Retry-After` 处理。
   对 429/529 这类"过一会儿就好"的错误，连 3 次背靠背失败就放弃。

而 `agent_loop.py:243` 收到 `LLMError` 后**直接 `yield error; return`** ——
主循环结束，整轮对话结束。对单 agent 而言这是"如实报错"，对 **team** 而言这是灾难：
lead 死了，但它 spawn 的队友还在跑、任务板还有 `pending` 任务、用户看不到任何结论。

### 1.3 第二类缺陷：lead 提前收工，队友的结果没人接

同一现场还暴露了另一条路径：`statuses` 显示 lead 在「等 reader」时就结束了自己那一轮。
即使那次模型调用是正常的，也存在这个时间窗口 —— **lead 一轮结束，而队友这一轮还没结束**。

`_merged_events` 会保住 SSE 不关（`has_active()`），但它保住的只是「事件通道」，
**没有任何机制把控制权交回 lead**。队友结果到了，进入 lead 的 inbox，
而 lead 的循环已经结束 —— 于是消息永远躺在库里。

这两类缺陷合起来就是用户的原话：
> 一个 agent team 本身就复杂，其中一些 agent 执行出问题是正常的，
> 但是整个团队不能崩溃或者退化。

## 2. 对标：GA 与 CC319 怎么做的

### 2.1 CC319

| 机制 | 位置（`reconstructed319/deminified`） | 要点 |
|---|---|---|
| 错误分类 | `m0591:126056 pLr()` | 逐条判定哪些错误可重试（408/409/429/5xx/`overloaded_error`/连接错误），哪些**永不重试**（额度耗尽、org 级禁用、`x-should-retry: false`） |
| 重试计数与预算 | `m0591:126353 X$()` | `retries < maxRetries` 才重试；`CLAUDE_CODE_MAX_RETRIES` 可覆盖且**有上限钳制**（`Ist`） |
| 降级重试 | `m0591:126373 pOe()` | `retryWithoutStreaming` —— 流式失败就退成非流式再试一次，这是"已经输出过内容"时的关键出路 |
| 部分结果保留 | `m0591:126420 G()` | `keepPartial` —— 有已达成的部分结果就保留，而不是整轮丢掉 |
| 经验性上限 | `m0591:126130` | `TLr = 1800000`（30 分钟）、`Fst = 20000`、`ELr = 600000` 等时间窗常量，防止无限重试 |

**CC319 最重要的一条**：它不把「已经输出内容」当成放弃重试的理由，而是**换一种方式重试**
（`retryWithoutStreaming`）。这正好是 Hlaudz 缺的那一块。

### 2.2 GenericAgent

| 机制 | 位置 | 要点 |
|---|---|---|
| 可重试集合 | `llm/core.py:658` | `_RETRYABLE = {408,409,425,429,500,502,503,504,529}`（与 Hlaudz 现有集合一致） |
| 重试状态外露 | `llm/core.py:634 _notify_retry_status()` | 重试**不写进对话**，只推一个瞬时 banner（`retrying` / `recovered`）；"the banner is transient UI, the conversation stays clean" |
| 尊重 `Retry-After` | `llm/core.py:660` | 优先用上游给的 `retry-after`，否则指数退避 |
| 溢出重试 + 回滚 | `core/loop.py:351 _rollback_overflow()` | 失败尝试写入 history 的内容**全部撤销**，保证重试不会把同一条 user message 送两遍 |
| 只在无副作用时静默重试 | `core/loop.py:63` | "The retry boundary can re-issue the request silently only while nothing has [been committed]" |

GA 的 `_rollback_overflow` 思路是 Hlaudz 必须照搬的：**重试边界必须能回滚**。

## 3. 设计：三层韧性

三层从内到外，各管一段，互不替代：

```
L1 调用层（llm_client）     —— 网络/HTTP 级自愈：退避重试、Retry-After、non-stream 降级
L2 循环层（agent_loop）     —— 模型调用彻底失败后：有限次"续跑"，保住已完成的工具工作
L3 团队层（team）           —— lead 提前收工时：把控制权交回去；队友失败不影响团队
```

### 3.1 L1：调用层自愈（`llm_client.py`）

| 改动 | 现在 | 改为 |
|---|---|---|
| 已输出内容后断流 | 放弃 | **降级非流式重试一次**（CC319 `retryWithoutStreaming`） |
| 重试预算 | 固定 2 次 | 默认 **4 次**，可用 `HLZ_LLM_MAX_RETRIES` 覆盖，**上限 8**（钳制，防误配无限重试） |
| 退避 | 无（背靠背） | 指数退避 + 抖动：`0.8s → 1.6s → 3.2s → 6.4s`（封顶 8s），**尊重 `Retry-After`** |
| 429/529 | 与 5xx 一视同仁 | 单独识别，退避更保守（走 `Retry-After`） |
| 不可重试 | 只有 HTTP 非 retryable | 同样保留：400/401/403/404/422 **一律不重试**（配置/权限问题重试无意义） |
| 可观测 | 只有最终 `LLMError` | 每次重试 `logger.warning` + `team_event("llm_retry", ...)` |
| 总时长闸 | 无 | 单次 `run()` 的累计重试等待 **封顶 180s**（CC319 的时间窗思路），超了就当失败 |

**关键约束：重试必须幂等。** `run()` 本身不写 history（history 由 `agent_loop` 管），
所以调用层重试天然安全 —— 这一点已经在现有结构里成立，实施时不能破坏它。

### 3.2 L2：循环层续跑（`agent_loop.py`）

调用层彻底失败（重试预算用尽）后，不再直接结束整轮：

| 情况 | 现在的行为 | 改为 |
|---|---|---|
| `LLMError`，本轮**已执行过工具** | `yield error; return` | **续跑**：把「上次调用失败，请继续」作为一条 system 提示注入，重试整个模型调用，最多 **2 次** |
| `LLMError`，本轮**什么都没做** | 同上 | 同样续跑（没有副作用，更安全） |
| 续跑次数用尽 | — | `yield error` 并结束，但**带上已完成的工作摘要** |
| 用户已 stop | — | 立即结束（不续跑） |

续跑必须满足的**不变量**：
- 工具已经执行过的结果**不能重跑**（工具可能有副作用：写文件、跑命令）。因此续跑只是
  **重新发起模型调用**，不重跑任何工具 —— history 保持原样，模型看到自己的工具结果后继续。
- 续跑要写入可见事件（`yield {"type": "llm_retry", ...}`），让前端和日志都能看到"正在自愈"。

### 3.3 L3：团队层不崩（`team`）

这是用户最关心的那一条。三条子机制：

#### L3-a：lead 提前收工时，把控制权交回去

**问题**：lead 一轮结束（模型不再调工具），但队友还在跑 → 队友结果没人接。

**修法**：`agent_loop` 的「无 tool_calls → 准备结束」出口增加一个**待续跑闸**：

```
if not tool_calls:
    if should_continue_after_team_work():   # 有活着的队友 / 有未消费的 inbox 消息
        history.append(assistant 这一轮的答复)
        注入一条 [团队仍在运行] 提示
        continue                            # 不 yield done，继续下一轮
    yield done
```

判定条件（满足任一即续跑）：
1. `host.has_active()` 为真（有队友在跑）；
2. 团队收件箱里还有未消费消息；
3. 任务板上还有 `in_progress` / `pending` 任务且本轮是自己派的。

**边界**（必须写死，否则会变成死循环）：
- 续跑上限 **`HLZ_LEAD_TEAM_GRACE_TURNS`（默认 3）**；
- 队友全部结束后**立即**收工，不再续跑；
- 用户 stop 立即收工；
- 续跑时如果这一轮什么都没干（没有新消息、没有队友状态变化），不续跑。

#### L3-b：队友失败不影响团队

现状核查结论：**已经成立**，需要的是补测试固定住。

- 队友 `errored` 时，`SubagentHost` 只记录自己的 `status=errored` + 通知 lead；
- team 的 `task` 会停在 `in_progress`（owner 已死）；
- lead 通过 `wait_agent` 看到 `errored`，可以 `task_update` 重新派活。

要补的：**失败任务可被重新认领**。现在 `bind_task_owner` 拒绝已绑定的任务，
但 owner 已经 `errored` 时应该允许重绑。这是"任务重派"的关键。

#### L3-c：LLM 中断可观测

`team_event("llm_retry", ...)` + `team_event("llm_gave_up", ...)`，
让 `tools/team_timeline.py` 能直接看出"这次协作里有过几次 API 中断、恢没恢复"。

## 4. 实施顺序

| 步 | 改动 | 验证 |
|---|---|---|
| 1 | `llm_client`：退避+抖动、`Retry-After`、预算 4（可配、上限 8）、总时长闸、重试日志 | 单测：假 httpx 返回 429 带 `Retry-After`，断言等待与取出重试；预算用尽后抛错 |
| 2 | `llm_client`：已输出后降级为非流式重试 | 单测：流式吐 3 个 delta 后断流 → 断言再次请求且 `stream=false` |
| 3 | `agent_loop`：`LLMError` 后有限续跑 | 单测：假 client 第 1 次抛 LLMError、第 2 次正常 → 断言产出 text 且工具只执行一次 |
| 4 | `agent_loop` + team：lead 提前收工的待续跑闸 | 单测：队友 running 时模型无 tool_calls → 断言不 yield done、注入提示、续跑；队友结束后立即收工 |
| 5 | `team_store`：owner 已结束后允许任务重绑 | 单测：owner `errored` → `bind_task_owner` 成功 |
| 6 | `team_timeline`：识别 `llm_retry` / `llm_gave_up` | 手工 |
| 7 | 真实湛卢 e2e：注入 API 中断（临时把 base_url 指向会断流的代理 / 用错误 key 触发 401 不重试） | runId + 事件 |

## 5. 明确不做
1. **不做无限重试**。无限重试在 Web 多用户形态下等于占着配额挂死，必须有上限。
2. **不做"重跑到成功"的任务级重派循环**。任务重派是 lead 的判断（它能看到错误原因），
   平台只保证"可重派"，不替它做决定。
3. **不把重试写进对话历史**（GA 的做法）。重试是瞬时状态，写进 history 会污染上下文，
   让模型以为自己说错过。
4. **不做跨轮持久 worker**（原有约束不变）。

## 6. 风险

| 风险 | 缓解 |
|---|---|
| 续跑导致同一工具被执行两遍 | 续跑只重发模型调用，history 不动，工具结果已在 history 里 |
| lead 待续跑闸变成死循环 | 硬上限 3 轮 + 队友全结束后立即收工 + stop 立即收工 |
| 重试把额度烧光 | 预算上限 8、总等待封顶 180s、429 走 `Retry-After` |
| 降级非流式导致用户体验变差（无逐字输出） | 只在"流式已失败"时降级，且降级成功后会补齐完整文本 |
| 任务重绑被滥用（正常 owner 的任务被抢） | 只在 owner **终态且非 completed** 时允许重绑 |

## 6.5 判断准则：上游不稳定期，什么才算代码缺陷（2026-11-11 补充）

湛卢上游目前**不稳定，时不时 502 / 504**。这会给验证带来真实的干扰：
一次慢、一次失败、一次多轮，很可能只是上游在抖，不是我们的 bug。

因此定一条硬性判断准则，**只有满足以下任一条件才判定为代码缺陷**：

| 判据 | 具体做法 |
|---|---|
| **脱离上游可复现** | 用假 client / MockTransport 写出稳定复现的用例，跑它必红。写不出来就不算 |
| **地图错位** | 结构不变量被破坏 —— 任务 owner 指向不存在的 agent、消息永久停在 `queued`、SSE `done` 不是最后一帧、agent 状态与 run 表不一致 |
| **自愈机制本身失效** | 出现了 502 但日志里**没有** `llm_retry`；或重试次数与配置不符；或 `agent_loop_continue` 没接上 |

**明确不算缺陷的现象**（上游抖动期的正常表现）：

- 总耗时变长（562s 里大部分是退避等待）；
- 某个 agent 的 turns 偏高（worker_b 用了 23 turns，是在跟中断周旋）；
- 单次 e2e 失败、下次又成功 —— 这是上游波动，不是产品问题；
- 模型输出的内容质量下降（上游降级时常见）。

**做法上的调整**：

1. 真机验证只看**结构性结论**（消息是否送达、任务 owner 是否正确、事件是否齐全），
   不看耗时和单次成败；
2. 任何"看起来像 bug"的现象，先查 `team-<date>.jsonl` 里有没有对应的
   `llm_retry` / `llm_gave_up`；有就是上游，没有才继续查代码；
3. 关键结论必须有一条**不依赖网络**的单测兜底，否则结论不成立。

## 7. 变更记录

- 2026-11-11：首版。基于真机 runId `111915-64a6`（927.9s 僵死、任务 3 永久 pending）
  与 CC319 `m0591` / GA `llm/core.py` 的对照分析。

## 8. 实施记录（已完成）

### 8.1 L1 调用层（`llm_client.py`，单测 552 → 561）

| 改动 | 说明 |
|---|---|
| 断流检测 | 流结束但既无 `[DONE]` 也无 `finish_reason` → 判定为截断，不再当成功返回半截内容 |
| 非流式降级 | 已吐过 delta 后失败，降级成 `stream:false` 再取一次完整结果（CC319 `retryWithoutStreaming`） |
| 指数退避 | `0.8 → 1.6 → 3.2 → 6.4s` 封顶 8s，带抖动；优先听上游 `Retry-After` |
| 重试预算 | 默认 4，`HLZ_LLM_MAX_RETRIES` 可覆盖，**硬上限 8** |
| 总等待闸 | 单次 `run()` 累计等待封顶 180s，超了当失败 |
| 不可重试 | 400/401/402/403/404/405/413/422 一律不重试（配置/权限问题重试无益） |
| 可观测 | `llm_retry` / `llm_retry_degraded` / `llm_gave_up` 进团队 JSONL |

**发现并修掉的装配缺陷**：`app/llm.py:49` 与 `app/config.py:134` 各自硬编码了
`max_retries`（2 和 3），而用户模型配置表 `user_llm_configs` **根本没有这个字段**，
于是 `HLZ_LLM_MAX_RETRIES` 在这两条装配路径上完全失效 —— 运维以为调到 4 了，
实际只重试 2 次。真机 502 时日志里 `attempt` 只到 2 就是这个原因。
两处均改为「配置 > 环境变量 > 默认 4」，并区分「缺省」与「显式 0」。

### 8.2 L2 循环层（`agent_loop.py`，单测 561 → 567）

`LLMError` 不再直接结束整轮，而是**有限续跑**：注入一条可执行的提示
（「接口中断不是你的错，从断点继续，已执行的工具结果都在上下文里」），
重发模型调用，默认 2 次、上限 3 次（`HLZ_AGENT_LLM_RETRIES`）。

**幂等性证明**：续跑只重发模型调用，history 一律不动 —— 工具结果已经在
history 里，因此工具绝不可能执行第二遍。测试 `test_continue_does_not_rerun_tools`
用「写文件」工具把这一条钉死。

用户已 stop 时不续跑。

### 8.3 L3-a lead 待续跑闸（`agent_loop.py` + `subagent/session.py`，单测 567 → 578）

`AgentLoop` 新增可选 `team_gate`：模型不再调工具、准备收工时先问一句
「团队还有没有没跑完的活」。有活就注入提示并续跑，硬上限 3 轮
（`HLZ_LEAD_TEAM_GRACE_TURNS`，上限 10）。

`subagent/session.py` 提供的闸实现返回可读原因：
- 有队友在跑 → 「还有 N 个队友正在运行（xxx），结果尚未返回。」
- 队友都在 idle → 「xxx 仍在待命，你可以继续用它干活，或者用 close_agent 释放槽位。」

闸自己抛异常时按「没活」处理（不能因为闸坏了卡住对话）。

### 8.4 L3-b 任务不烂在死 agent 手里（`team_store.py` + `subagent/host.py`，单测 578 → 583）

两个互补机制：

1. **失主接管**（`bind_task_owner`）：原 owner 已处于 `errored/stopped/closed/cancelled`
   终态时，允许直接重绑（`task_owner_stale_rebound` 事件）。
   `completed` 不算死 —— 那说明它干完了。仍然守 `blocked_by`。
2. **自动释放**（`_release_dead_agent_tasks`）：队友以 `errored/stopped` 结束时，
   平台自动把它占的 `in_progress` 任务 reclaim 回 `pending`（`task_released_on_failure` 事件）。

第 2 条是这次真机跑出来的：runId `121743-7329` 里两个 worker 因上游持续 502 而
errored，任务永远停在 `in_progress`，第三个任务永远等不到依赖完成 —— 团队直接退化。

### 8.5 L3-c 可观测（`tools/team_timeline.py`）
时间线新增韧性事件列，并在诊断段输出：
`ℹ API 中断自愈：N 次重试，M 次放弃`、`ℹ lead 因队友未完成而续跑 N 次`。
「重试全成功」时明确打印 `✓ 全部自愈成功，没有中断泄漏到用户`。

### 8.5b 幂等性：重做计划不能长出僵尸任务（单测 586 → 592）

真机 runId `125016-824c` 里发现任务板上多出 2 个永远没人认领的 `pending` 任务。
追下去是两处本地逻辑缺陷，**与上游 502 无关**（能脱离网络稳定复现）：

| 缺陷 | 现象 | 修法 |
|---|---|---|
| **`reclaim` 对已释放任务报错** | lead 对一个「pending 且无 owner」的任务调 `reclaim`，平台返回无意义的 `conflict`；模型据此以为释放失败 | 目标状态已达成时返回 `ok + already_released`（幂等），不再报 conflict |
| **`create_task` 无去重** | 模型因上一步"失败"重做整段计划，任务板上出现同名重复行 | 同团队内同名且未完成（`pending`/`in_progress`）时复用已有任务，写 `task_create_deduped` 事件 |

两条连起来才是那次的现象：`reclaim` 误报失败 → lead 以为要重建 → 重建又不拦 →
多出 2 行。只修一条都不够。

边界：`complete` 过的任务允许同名新建（那是新一轮工作）；去重严格按 team 作用域，
不同团队的同名任务互不影响。

### 8.6 真机验证（真实湛卢 API，上游正在持续 502）

这些验证赶上了一次**真实的上游 502 中断**，比构造故障更有说服力：

| runId | 观测 |
|---|---|
| `121743-7329` | 韧性链路完整走通：`llm_retry`（退避）→ `llm_gave_up`（调用层用尽）→ `agent_loop_continue`（循环层续跑）→ 再重试。两个 worker 最终 errored，任务板被 L3-b 兜住 |
| `123949-4978` | worker_a 正常 `completed`；lead 自身 502 反复重试；任务释放逻辑生效 |
| `125016-824c` | **修复重试预算后**：三个 worker 全部 `completed`、三个任务 owner 正确、三条消息全 `consumed`、时间线无告警。总耗时 562s（大部分是退避等待），`worker_b` 用了 23 turns 跟中断周旋 —— 按 §6.5 的准则，这些属于上游抖动的正常表现，不是缺陷 |

日志证据（`team-20261111.jsonl`）：`llm_retry` 的 `waited` 值呈 0.68 → 1.21 → 1.64
的退避爬升，与设计一致。

**结论按 §6.5 判定**：`125016-824c` 满足全部结构性不变量（owner 正确、消息
`consumed`、无滞留、`done` 为末帧），因此判为通过；耗时不作为判据。

### 8.6b 上游彻底放弃时的收尾摘要（单测 597 → 608）

上面 §6.5 说「耗时长不算缺陷」，但真机 `131321-558a` 暴露了一个**真正的 UX 缺口**：
三条任务全部 completed、三个 worker 都正常结束，lead 却在收尾阶段撞上持续 502，
用户最后看到的只有一句半截话 —— 劳动成果都在任务板和数据库里，却没有任何东西告诉用户。

新增 `backend/app/partial_summary.py`，在 SSE 的 `error` 分支追加一段收尾：

```
> **执行中断说明**
> 你的请求「…」在收尾阶段被上游模型服务中断打断。

**中断前已完成**
- ✅ 任务1：统计 backend/app 目录下的 .py 文件数量（worker_a）
- ✅ 任务2：计算 backend/app 目录下 .py 文件平均行数（worker_b）

**尚未完成**
- ⏳ 任务3：用一句话总结前两步结论（未开始）

**执行失败的队友**：worker_c

**接下来可以怎么做**
- 直接重发一次即可：上面的进度都在任务板和上下文里，不会丢。
```

**核心约束：这段绝不调用模型。** 它存在的场景就是模型服务已经不可用 ——
任何依赖 LLM 的兜底都会跟着一起失败。所以全部是数据库读取 + 模板拼装，
并用 `test_summary_does_not_call_llm` 把这条钉死（monkeypatch 让 `LLMClient.run`
和 `build_client_from_config` 直接抛异常，摘要仍要能生成）。

**实施中发现的缺陷（两个，都是真机跑出来的）**：

1. 第一版只写 `blocks` 没写 `buffer`，导致摘要**流里看得到、刷新后消失**
   （`_persist` 的 `content` 取自 `buffer`）。真机 `134210-0e77` 复现，
   已修复并补 `test_summary_text_reaches_persisted_content` 固定「两处必须同步」。
2. 摘要只查**当前 generation** 的任务板，而用户说「再总结一下刚才的结果」时，
   成果属于**上一代** —— 于是什么都不汇报，恰恰在他最需要知道
   「上一轮成果没丢」的时刻。真机 `142451-cd65` 复现。
   已加跨代回退：本轮无成果时回溯本会话最近一次有产出的一代
   （`_latest_finished_generation`），并有 `test_fallback_does_not_invent_history_for_stranger_session`
   保证全新会话不会凭空编出历史。

**真机验证**（`143106-ffcf`，用不可达 base_url 稳定构造 error 路径）：
第二轮 `done=error`，摘要同时出现在 SSE 流**和**持久化内容里，正确列出上一代成果
「统计 backend/app 下 .py 文件数（counter）」并给出下一步建议。

### 8.7 测试总账

| 层 | 文件 | 条数 |
|---|---|---|
| L1 | `tests/test_llm_resilience.py` | 12 |
| L2 | `tests/test_agent_loop_resilience.py` | 11 |
| L3-a | `tests/test_lead_team_gate.py` | 7 |
| L3-b | `tests/test_team_failover.py` | 4 |
| L3-b | `tests/test_team_task_release.py` | 5 |
| 幂等 | `tests/test_team_idempotency.py` | 6 |
| 收尾 | `tests/test_partial_summary.py` | 11 |

后端全量 **552 → 608**。十三组变异反向确认全部 RED。