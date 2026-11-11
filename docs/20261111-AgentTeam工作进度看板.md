# Agent Team 工作进度看板

> 日期：2026-11-11（持续更新）　用途：**回头查看"做到哪一步了"**，避免翻多份长文档
> 详细内容见各阶段文档，本文只记**状态、坐标、结论、下一步**

## 1. 当前坐标

| 项 | 值 |
|---|---|
| 分支 / HEAD | `main`（F1–F3 已提交 `d47aa3b`；韧性三层未提交） |
| 后端单测 | **606 passed** |
| 前端构建 | 通过 |
| 持久化开关 | `TEAMMATE_IDLE_TIMEOUT_S`（**默认 0 = 关闭**，行为与历史一致） |
| 日志 | **默认开启**（`HLZ_LOG_LEVEL=INFO` + team JSONL），关：`HLZ_LOG_LEVEL=OFF` / `HLZ_TEAM_LOG=0` |
| 韧性开关 | `HLZ_LLM_MAX_RETRIES`（默认 4，上限 8）/ `HLZ_AGENT_LLM_RETRIES`（默认 2，上限 3）/ `HLZ_LEAD_TEAM_GRACE_TURNS`（默认 3，上限 10） |
| AGENTS.md 待更新 | 是 |

## 2. 三份文档的分工（先看这个再决定读哪份）

| 文档 | 是什么 | 什么时候读 |
|---|---|---|
| **本文** | 进度看板：做到哪、下一步做什么 | **每次接手先看这个** |
| `docs/20261111-AgentTeam协作硬制度加固.md` | **D1–D4 已完成**：消息必达、依赖硬约束、owner 绑定、收件人归一化 | 想知道"协作为什么现在可靠了" |
| `docs/20261111-AgentTeam持久化Worker分析与设计.md` | **设计与 P-A 实施记录** + §4.5 的 7 处漏洞复核 | 想改持久化 / 想知道坑在哪 |

## 3. 已完成（按时间倒序）

### ✅ 团队韧性（Resilience）三层自愈（本轮，单测 552 → 586）

起因：真机跑多智能体链路时，lead 因一次 LLM API 中断僵死 **927 秒**，
任务 3 永久停在 `pending`，哨兵没复述。事后定位到两层缺陷，补成三层韧性。

| 层 | 机制 | 关键点 |
|---|---|---|
| **L1 调用层** | 退避重试 + `Retry-After` + **断流检测** + **非流式降级** | 旧实现「一旦流里出过 delta 就不再重试」是僵死根因；新实现改走 CC319 的 `retryWithoutStreaming` |
| **L2 循环层** | `LLMError` 后**有限续跑** | 只重发模型调用，**不重跑工具** —— 工具结果已在 history，天然幂等 |
| **L3 团队层** | lead 收工前的**待续跑闸** + **任务不烂在死 agent 手里** + **幂等** | 队友还在跑就不许 lead 收工；队友 errored 时平台自动 reclaim 它占的任务；`reclaim` 对已释放任务幂等成功、`create_task` 同名去重 |
| **收尾** | 上游彻底放弃时生成**已完成工作摘要** | 纯读库拼装、**绝不调模型**（此刻模型正不可用）；任务完成情况 / 失败队友 / 下一步建议 |

**顺带挖出一个隐蔽的装配缺陷**：`app/llm.py` 与 `app/config.py` 各自硬编码
`max_retries`（2 和 3），而用户模型配置表 `user_llm_configs` **根本没有这个字段**，
于是 `HLZ_LLM_MAX_RETRIES` 完全失效 —— 运维以为调到 4 了，实际只重试 2 次。
两处都改成「配置 > 环境变量 > 默认 4」，并区分「缺省」与「显式 0」。

**验证特别有说服力**：验证期间上游湛卢 API 正在真实地持续返回 502，
不是构造故障。日志里能看到完整的自愈链路
`llm_retry`（退避 0.68→1.21→1.64s）→ `llm_gave_up` → `agent_loop_continue` → 再试。
两个 worker 最终 errored，但任务板被 L3-b 兜住没有烂掉。

详见 `docs/20261111-AgentTeam韧性设计与实施.md`（含 CC319 `m0591` /
GA `llm/core.py` 的逐条对照）。

### ✅ 真实使用观察（本轮，持久化开着跑真机）

开着 `TEAMMATE_IDLE_TIMEOUT_S=45` + 默认日志，用真实湛卢跑三个贴近日常的场景
（`iterative` 分两步追问 / `pipeline` 三任务依赖链 / `steer` 中途改需求），
**抓到 3 个单测原理上测不到的链路缺陷**：

| # | 缺陷 | 现象 | 根因 |
|---|---|---|---|
| F1 | **lead 关掉已干完活的空闲队友，被记成 `stopped`** | 前端 `SubagentCard` 显示「已停止」，用户以为自己打断了它；实际它这轮做完了、结果也交了 | `_stop_node` 只对 `reason="drain"` 补偿成 `completed`，`reason="close"` 直接落 `stopped`。`close()` 还会无条件盖成 `closed` |
| F2 | **实时投递过的消息会被二次注入** | `send_message` 实时推给活体后库里只标 `delivered`，下一轮 `_refill_inbox` / `deliver_pending_inbox` 又把 `state IN ('queued','delivered')` 全取一次 | 状态机少推进一格：已进对方面前队列的消息，库里必须同步成 `consumed` |
| F3 | **时间线工具误报「消息滞留」** | teammate→lead 的实时投递走内存通知队列，不记 `message_delivered`；实时投递成功也不记 `inbox_consumed` | 成功分支没有埋点，`team_timeline.py` 只能看到 `message_queued` |

F2 是最值得记的：它不是投递逻辑写错，而是**「实时投递」与「补投」两条路径共用一张
状态表却不同步推进**。真机 `steer` 场景里那条 `root → sub:writer` 停在 `delivered`
就是它的痕迹 —— 当时看着只是"状态没更新"，实际是下一轮会把同一件事再安排一遍。

**修法**：
- `_stop_node` 增加 `settled = was_idle and not has_pending_work`，`close` 也走 completed 分支；
  `close()` 改成 `if not node.is_terminal` 才盖 `closed`；
- `registry._evict_one_idle` 淘汰空闲节点时同样先判 `completed`（槽位回收不是打断）；
- `mark_messages_delivered` 直接把状态推到 `consumed`（改语义并重命名注释）;
- `team_host._deliver` 成功分支补 `message_delivered`（带 `logged` 标志去重），
  实时投递成功后补一条 `inbox_consumed`（标 `via=live_delivery`）。

**验证**：单测 541 → **550**（新增 6 条：close idle / close 有 pending / close running /
槽位淘汰 / 队友侧重复注入 / lead 侧重复注入），两组 stash 反向确认全 RED。
真机复跑：`pipeline` 三个 worker 一致 `completed`（修复前 worker_c 是 `stopped`）、
`steer` 的 root→writer 消息变 `consumed`、`iterative` 时间线每条消息只出现一次。

### ✅ 日志基础设施（本轮）

**发现的问题比预想严重**：项目里有 29 处 `logger.*` 调用，但**从未配置过 handler**，
uvicorn 只管自己的 logger，`app.*` 的日志全部被静默丢弃。所以多智能体协作出问题时
根本没有痕迹可查 —— 只能靠 print 和猜。

新增 `backend/app/logging_setup.py`：

| 能力 | 开关 | 说明 |
|---|---|---|
| 应用日志可见 | `HLZ_LOG_LEVEL=INFO` | 默认 WARNING，不改变现有噪音 |
| 应用日志落文件 | `HLZ_APP_LOG=app.log` | 按 20MB 轮转，留 5 份 |
| **团队事件 JSONL** | `HLZ_TEAM_LOG=1` | `backend/data/logs/team-<日期>.jsonl`，50MB 轮转留 10 份 |

团队事件是**每行一条 JSON**，字段平铺，可直接 grep `message_id` / agent 名：

```
{"ts":"...","event":"message_delivered","agent":"keeper","message_id":"tmsg_...",
 "sender":"root","was_idle":true,"pending":1}
```

埋点覆盖了协作的完整链路：`message_queued` → `message_delivered` / `message_undelivered`
→ `inbox_consumed` → `agent_round_done` → `agent_idle_wait` → `agent_end`，
外加 `task_claimed` / `task_completed` / `task_complete_failed`（带 `actual_owner`，
owner 不匹配是最隐蔽的一类失败）/ `task_bind_blocked` / `delivery_failed`。

**为什么不另开一个 agent 监测**：另一个 agent 看不到别人的运行时，它真正能做的还是
"读日志"。要排查竞态、消息丢失、状态不一致，必须在事件发生的那一刻记录事实，
事后靠另一个 agent 猜是做不到的。**先有日志，才谈得上多一双眼睛。**

**验证**：单测 535 → 541（新增 6 条，含"应用 logger 真的有 handler"这条核心回归）；
真机跑通两个场景，34 条事件完整复盘了一次协作（见上）。

### ✅ P-A 边界摸排

在 P-A 落地后专门摸排"实现能跑之后真正会出问题的地方"，**抓到 3 个新缺陷**：

| # | 缺陷 | 现象 | 修法 |
|---|---|---|---|
| E1 | **竞态：idle 但有待处理消息时 `wait` 提前返回** | `send_message` 投递后节点仍是 `idle`，要等循环唤醒才变 `running`。这一瞬间 `wait(all_terminal)` 会把"活还没开干"误判成"已了结"，返回给 lead 过期的结论 | `is_settled` 改为 `is_idle and not has_pending_work` |
| E2 | **重复投递无幂等** | 重连 / 循环重跑 / `_refill_inbox` 补投都可能把同一条消息投两次，队友会把同一件事做两遍 | `AgentNode.delivered_ids` 按 `message_id` 去重（上限 500 条后清空） |
| E3 | **`_idle_wait` 忙循环** | 兜底轮询写的是「有待处理消息就 wake」，一条没被消费的消息会让它每次轮询立刻返回（实测 2 秒 34 次空转） | 改判「数量比进入时多了」才算新活 |

E1 是最值得记的：它不是"实现写错"，而是**持久化引入的新时间窗口**——
状态机里存在一个"已 idle 但已经有活排队"的瞬时状态，旧的二值判定看不到它。

同时补了两处可观性：
- `wait_agent` 的工具描述补上 `state=ready` 的语义与后续动作；
- `to_dict()` 暴露 `rounds` / `idle` / `pending_messages`，模型能区分"该取结果"还是"该关掉"。

**验证**：单测 524 → 535（新增 11 条边界）；3 组反向确认全 RED；
真实湛卢 e2e 两轮：`persist2-094607-a682`（多队友并发 7/7）+
`persist-095040-487f`（单队友 5/5）。
多队友场景里 `alpha`/`beta` 各自经历 `idle→running→idle`、各跑满 2 轮、
各自保住自己的数字（111 / 222 未串），SSE 的 `done` 仍是最后一帧（未被 idle 拖住）。

### ✅ P-A 持久化 worker 骨架（`9ec0ba3`，2026-11-11）

**做了什么**：队友跑完一轮不结束，进 `idle` 等后续；被消息唤醒后**带着上下文**继续。

**关键修法**（对应设计文档 §4.5 的 7 条漏洞）：

| # | 漏洞 | 修法 | 反向确认 |
|---|---|---|---|
| 1 | `wait(all_terminal)` 被 idle 卡死 | `is_settled` 判定 + `ready` 态 | RED ✅ |
| 2 | `read_agent_result` 拒绝读 idle | 允许，返回 `partial:true` | RED ✅ |
| 3 | `subagent_runs` 只写一次 | `update_run_progress()` + `rounds` 列 | 单测 |
| 4 | 槽位泄漏（idle 不可回收） | `_evict_one_idle` 两级优先 | RED ✅ |
| 5 | SSE 被 idle 队友拖 300s | 拆 `has_active`/`has_live_nodes` | RED ✅ |
| 6 | `followup` 5 条封死长期 worker | 按未消费条数限流 | RED ✅ |
| 7 | idle 通知与终态通知混 | 分开 + 已 DM 则抑制 | 正反对照 ✅ |

**实施中发现的新问题**（复核时没预见到）：
`stop_all_now()` 抢在 `_stop_node(drain)` 前无条件 `task.cancel()`，导致子任务走
`CancelledError` 分支写 `stopped`，**覆盖了"它干完过 2 轮"的事实**。修法：三处加
`if not node.is_terminal` 判断 + `stop_all_now` 对 `rounds>0` 先判 `completed`。

**证据**：真实湛卢 e2e runId `persist-093445-edf2`，5/5 PASS。
`starts=['keeper']`（只 spawn 一次）、`statuses=[idle, running, idle]`、
第二轮答出第一轮写进上下文的 `4242`、`runs={status:completed, rounds:2, turns:5}`。

### ✅ D1–D4 协作硬制度（`52e40d0`，2026-11-11）

| # | 缺陷 | 修法 |
|---|---|---|
| D1 | `blocked_by` 只在 `claim` 检查，`spawn_teammate` 不检查 | 共用 `task_is_blocked()`，spawn **之前**就拒 |
| D2 | 同名复用后 owner 绑到旧名字，活着的 agent 无法 complete | 先 spawn 拿**真实** `agent_id` 再绑定 |
| D3 | lead 发 `poet`、队友收件箱是 `sub:poet`，消息永久读不到 | `normalize_agent_id()`，写读同一函数 |
| D4 | `send_message` 只写库、从不投递 | `queued→delivered→consumed` 状态机 + 名字钉住表 |

**证据**：真实 e2e T1–T11 共 35/35 + 12/12 + 8/8 + 3/3 + 5/5 PASS；单测 488→505。

### ✅ 赞/踩图标改用 claude.ai 造型（`8129e8c`）

从 Anthropicons 字体（U+E0FB / U+E0F9）提取轮廓，`SolidIcon` 组件承载。

## 4. 未完成（按建议优先级）

| 阶段 | 内容 | 建议 |
|---|---|---|
| **P-B** | 自动认领：`find_claimable()` + CC prompt 模板 + `TEAMMATE_AUTO_CLAIM`（默认关） | **下一步**，但要先观察 P-A 的实际使用 |
| **P-C** | idle 通知帧的结果预算（单条 4000 / 总 16000 截断） | P-B 之后 |
| P-D | 钩子框架 | **不做**（成本在配置面与 Web 多用户权限，与 team 不耦合） |
| — | 跨轮持久 worker | **不做**（需后台任务系统，另一条产品线） |

## 5. 已知限制（写清楚，避免以后误判为 bug）

0. **上游湛卢 API 目前不稳定，时不时 502 / 504**。判断"是不是代码缺陷"必须先过这一关：
   - **不算缺陷**：总耗时变长、单次 e2e 失败、某 agent turns 偏高、模型输出质量下降；
   - **算缺陷**：脱离上游能复现（假 client 必红）、结构性不变量被破坏（owner 指向不存在的
     agent / 消息永久 `queued` / `done` 不是末帧）、**自愈机制本身没生效**（有 502 但日志里
     没有 `llm_retry`，或重试次数与配置不符）。
   - 排查动作：任何"像 bug"的现象，先查 `team-<date>.jsonl` 里有没有 `llm_retry` /
     `llm_gave_up`；有就是上游，没有才继续查代码。
   - 详见 `docs/20261111-AgentTeam韧性设计与实施.md` §6.5。

1. **持久 worker 只活在"本轮 SSE 内"**。`_merged_events` 提供最长 300s 窗口；跨用户消息轮次
   不复用 agent（SSE 一关事件可见性就断）。这是 Web 形态的固有约束，不是缺陷。
2. **默认关闭**。要启用：`TEAMMATE_IDLE_TIMEOUT_S=45 python run.py`。
3. **自动认领未实现**。队友只会做被 `spawn_teammate` 绑定的任务，不会自己去抢任务板。
4. **`followup` 去重哈希上限 200 条**，防止超长寿命 worker 无界增长（超出后不再去重）。

## 6. 测试资产索引（work/ 会被清，但结论已落盘）

| 脚本 | 覆盖 |
|---|---|
| `_team_e2e_20261111_TR1.py` | T1–T4：基础、消息传递、依赖、跨轮 |
| `_team_e2e2_20261111_TR1.py` | T7 同名复用、T8 依赖绕过交叉验证 |
| `_team_e2e3_20261111_TR1.py` | T9 复杂交接、T11 运行中投递 |
| `_team_msg_e2e_20261111_TR1.py` | T5 自动送达、T6 lead→队友推送 |
| `_team_stop_20261111_TR1.py` | T10 停止级联 |
| `_persist_e2e_20261111_TR2.py` | **持久 worker**（P-A） |
| `_inj_ui_20261111_TR1.py` | 注入消息的 UI 可见性 |
| `_persist_e2e2_20261111_TR3.py` | 持久 worker：多队友并发 + idle 期间停止 |
| `_busy_probe_20261111_TR3.py` | `_idle_wait` 忙循环探测 |
| `_rc*_20261111_TR*.py` | 各阶段反向确认 |

单测：`tests/test_team_hard_contract.py`（17）、`tests/test_team_persistent_worker.py`（19）、
`tests/test_team_persistent_loop.py`（4）、`tests/test_team_idle_edges.py`（13）、
`tests/test_logging_setup.py`（9）。

## 7. 下一步计划（本轮继续）

1. ~~写进度看板~~（本文）
2. ~~深度测试 P-A~~：多队友并发、idle 停止、SSE 关闭时机、唤醒风暴 —— 已完成，抓到 E1–E3
3. ~~摸排遗留缺陷~~：本轮已完成一轮
4. **下一步**：P-B 自动认领（`find_claimable` + CC prompt 模板 + 默认关闭的开关）
5. 继续观察：P-A 在真实使用里的表现（尤其 idle 时长是否合理）

## 8. 变更记录

- 2026-11-11：首版，记录 D1–D4 + P-A 完成状态。
- 2026-11-11（第二轮）：P-A 边界摸排完成，抓到 E1–E3 并修复；单测 524 → 535。
- 2026-11-11（第三轮）：补日志基础设施（此前 29 处 logger 调用全部无效）；
  单测 535 → 541，真机验证协作事件流可复盘。
- 2026-11-11（第四轮）：日志改为**默认开启**；开着持久化跑真实使用，
  抓到 F1–F3（关闭语义 / 重复注入 / 时间线误报）并修复；单测 541 → 550。
- 2026-11-11（第五轮）：**团队韧性三层**（L1 调用层 / L2 循环层 / L3 团队层）
  完成并真机验证，赶上游真实 502 中断；顺带修掉 `max_retries` 硬编码导致
  环境变量失效的装配缺陷；单测 550 → 586。
- 2026-11-11（第六轮）：任务操作**幂等**（reclaim 对已释放任务成功、create_task
  同名去重，修掉僵尸任务）；上游报错**翻译成人话**；上游彻底放弃时生成
  **已完成工作摘要**收尾；单测 586 → 606。
