# Agent Team 工作进度看板

> 日期：2026-11-11（持续更新）　用途：**回头查看"做到哪一步了"**，避免翻多份长文档
> 详细内容见各阶段文档，本文只记**状态、坐标、结论、下一步**

## 1. 当前坐标

| 项 | 值 |
|---|---|
| 分支 / HEAD | `main` @ `9ec0ba3` |
| 后端单测 | **535 passed** |
| 前端构建 | 通过 |
| 持久化开关 | `TEAMMATE_IDLE_TIMEOUT_S`（**默认 0 = 关闭**，行为与历史一致） |
| AGENTS.md 待更新 | 是（本文档写完后再补） |

## 2. 三份文档的分工（先看这个再决定读哪份）

| 文档 | 是什么 | 什么时候读 |
|---|---|---|
| **本文** | 进度看板：做到哪、下一步做什么 | **每次接手先看这个** |
| `docs/20261111-AgentTeam协作硬制度加固.md` | **D1–D4 已完成**：消息必达、依赖硬约束、owner 绑定、收件人归一化 | 想知道"协作为什么现在可靠了" |
| `docs/20261111-AgentTeam持久化Worker分析与设计.md` | **设计与 P-A 实施记录** + §4.5 的 7 处漏洞复核 | 想改持久化 / 想知道坑在哪 |

## 3. 已完成（按时间倒序）

### ✅ P-A 边界摸排（本轮，未提交前）

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

单测：`tests/test_team_hard_contract.py`（17）、`tests/test_team_persistent_worker.py`（15）、
`tests/test_team_persistent_loop.py`（4）、`tests/test_team_idle_edges.py`（11）。

## 7. 下一步计划（本轮继续）

1. ~~写进度看板~~（本文）
2. ~~深度测试 P-A~~：多队友并发、idle 停止、SSE 关闭时机、唤醒风暴 —— 已完成，抓到 E1–E3
3. ~~摸排遗留缺陷~~：本轮已完成一轮
4. **下一步**：P-B 自动认领（`find_claimable` + CC prompt 模板 + 默认关闭的开关）
5. 继续观察：P-A 在真实使用里的表现（尤其 idle 时长是否合理）

## 8. 变更记录

- 2026-11-11：首版，记录 D1–D4 + P-A 完成状态。
- 2026-11-11（第二轮）：P-A 边界摸排完成，抓到 E1–E3 并修复；单测 524 → 535。
