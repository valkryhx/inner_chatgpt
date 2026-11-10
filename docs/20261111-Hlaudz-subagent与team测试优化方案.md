# Hlaudz subagent 与 Agent Team 充分测试与优化方案

- 日期：2026-11-11
- 分支：`feat/ga-cc-alignment`
- 父文档：`20261111-Hlaudz对标GA-CC优化总纲.md` §3 P0.5 / P1 / P2
- 对标材料：
  - GA `docs/20261108-GA-subagent异步与通信改造设计.md`
  - GA `docs/20261109-GA-subagent技术架构与实现说明.md`
  - GA `docs/20261108-GA-Ink与subagent四轮修复总览.md`
  - GA `docs/20261109-agent-team真实回归与UX验证.md`（6 轮真实运行）
  - CC 319：`claudecode283_hack/docs/20261108-CC-subagent异步与agent间通信机制调研.md`

---

## 0. 一句话结论

Hlaudz 的 subagent/team **架构是对的**（异步 Task、持久任务板、统一事件流、CAS 版本、
闭世界工具授权），但**验证方式不对**：36 个用例全部是进程内假对象，
从不经过「真 LLM → 真工具 → 真事件 → 真回收」链路。

GA 在同样「单测全绿」的状态下，用真实 e2e 连续抓出 12+ 个缺陷，其中多数是
**单测在原理上测不到**的：关闭竞态、僵尸状态、信号丢失、路径分裂。
本方案的第一交付不是代码，而是 **L3 真实 e2e 骨架**；先让它把缺陷逼出来，再修。

---

## 1. Hlaudz 现状（带坐标）

### 1.1 已经对的部分

| 能力 | 坐标 | 说明 |
| --- | --- | --- |
| 异步派生 | `subagent/host.py:66 spawn()` | `background=True` 立即返回 |
| 超时不杀 | `host.py:218 wait()` | 已有测试 |
| 闭世界授权 | `subagent/capabilities.py` | 编排工具不进子 registry |
| 事件合流 | `subagent/events.py` | 主/子共用一个队列 |
| 任务 CAS | `team_host.py:187 claim()` | `expected_version` |
| 任务依赖 | `team_host.py:164 create_task()` | `blocked_by` |
| 生命周期租约 | `team_host.py:102 acquire_lifecycle()` | 防并发重复收尾 |
| 停止级联 | `routers/chat.py` stop 分支 | 已有测试 |
| 用户隔离 | `team_store` / `subagent/store` | 已有跨用户测试 |

### 1.2 缺的部分

| 缺口 | 影响 |
| --- | --- |
| 无真实 LLM e2e | 链路缺陷不可见 |
| 无 steer（`followup_task`） | 派错只能关掉重派 |
| 无名字钉住 | 关闭后同名可被新 agent 复用，消息可能串 |
| 无通知 fold 注入 | 子代理完成只能靠 `wait` 主动取 |
| 无 `notify_when_idle` | 母代理无法「等它闲下来再说」 |
| `wait_agent` 阻塞回合 | 派 3 个活要等 3 次，期间母代理什么都做不了 |
| 事件只在内存 | 进程重启后 `event_seq` 归零，增量游标失效 |

---

## 2. GA 的缺陷目录（当作 Hlaudz 的排查清单）

GA 在 2026-11-08～11-09 修掉的问题，按「能不能在 Hlaudz 复现」分类。
**注意：这是假设清单，不是结论**；每一项都要用 L3 跑一遍再定性。

### 2.1 高概率同类（架构同构）

| # | GA 缺陷 | GA 的根因 | Hlaudz 对应风险点 | 状态 |
| --- | --- | --- | --- | --- |
| D1 | 通知父邮箱路径三方分裂，永不送达 | 三处各自猜路径 | `subagent/store.py` + `team_store.py` 是否共用同一路径构造 | 待核 |
| D2 | 关闭竞态把干完活的成员记成 failed | 收尾信号与完成信号竞争 | `host.py:330 close()` 与 `_drive()` 的完成写入 | 待核 |
| D3 | 已完成但仍在线的成员显示 running | 读侧不看活体权威 | `list_agents` 取的是 `node.status` 还是实时探测 | 待核 |
| D4 | 任务挂在已关闭成员上不归还 | 自释放路径漏归还 | `team_host.py` 关闭 teammate 时是否 `reclaim` 绑定任务 | 待核 |
| D5 | 通知只在用户 turn 开头消费，回合内到达的丢失 | 无 fold 点 | Hlaudz 无通知通道，暂不适用；但 `wait` 增量游标有同类风险 | 待核 |
| D6 | 消费是破坏性的，异常即丢 | pop 即删 | `host.py:218 wait` 的 `event_seq` 游标是否只读不删 | 待核 |
| D7 | `send_message` 投进已关闭 agent 的邮箱 | 无名字钉住 | Hlaudz team `send_message` 的目标校验 | 待核 |
| D8 | `notifications.jsonl` 只增不减 | 无 compaction | `subagent/store.py` 事件表是否无限增长 | 待核 |

### 2.2 中概率（Hlaudz 可能已规避）

| # | GA 缺陷 | Hlaudz 现状 |
| --- | --- | --- |
| D9 | 默认路径是 sleep 轮询 | Hlaudz 用 `asyncio.Event`，无轮询 |
| D10 | 子进程被强杀后任务判 failed | Hlaudz 是进程内 Task，不适用 |
| D11 | 管道名冲突 / 通道线程泄漏 | Hlaudz 无独立进程 |
| D12 | 工具异常掀翻 worker | 需确认 `_drive()` 的异常边界 |

### 2.3 已被 Hlaudz 明确处理

- 停止级联（`test_stop_endpoint_cascades_to_subagents`）
- 跨用户/跨会话不可见（`test_cross_user_and_session_not_visible`）
- 任务 CAS 冲突（`test_task_cas_conflict_and_dependency_blocked`）
- 规模槽位（`test_slot_limit_is_per_host`）

---

## 3. 更快逼近缺陷：静态可疑点清单

在跑 e2e 之前，先把「读代码就能看出可疑」的地方列出来，作为 e2e 的重点观察项。

### 3.1 `close()` 与完成写入的顺序

`subagent/host.py:330 close()` → `_stop_node()` → 设置 `node.stop`。
若子代理此刻正好在写 `node.result` / `node.status = "completed"`，
关闭路径是否会把它覆盖成 `"stopped"`？两条路径都需要读一次才能定性。

### 3.2 `_drive()` 的异常边界

`host.py:139 _drive()` 里若有未捕获异常，`node.status` 会不会停在 `"running"`，
导致 `wait` 永远等不到 terminal？

### 3.3 `wait()` 的游标语义

`host.py:218 wait(..., since_event_seq)`：游标是「已消费位置」还是「上次返回的最大 seq」？
如果是前者且消费是破坏性的，第二个等待者会看不到事件。

### 3.4 team 关闭时的任务归还

`team_host.py` 关闭 teammate 的路径（`set_status` / `spawn_teammate` 失败回滚）是否
调用了 `reclaim`。GA 的 D4 就死在这里。

### 3.5 事件表增长

`subagent/store.py` 的事件写表是否有上限或清理；一天跑 100 次 subagent 会怎样。

---

## 4. L3 真实 e2e 骨架（第一交付）

### 4.1 目录与产物

```
backend/tests/e2e/
  conftest.py            # 起真实后端（或直连 app）、真实 LLM 配置、workspace 隔离
  harness.py             # runCase()：发消息 → 收 SSE → 落盘 → 返回 runId
  cases/
    subagent_basic.yaml
    subagent_parallel.yaml
    subagent_steer.yaml
    team_basic.yaml
    team_scale.yaml
    team_cancel.yaml
  runs/<runId>/events.jsonl      # 全部事件
  runs/<runId>/summary.json      # 工具调用序列、耗时、终态
```

### 4.2 硬规则（GA 踩过的坑，直接写进 harness）

1. **不读 stdout 字符流做断言**。SSE 事件按行落盘，事后按 `type` 过滤。
2. **每次运行生成唯一 `runId`**，所有断言都带 `runId`，避免读到上一次的残留。
3. **哨兵 token**：给子代理的任务里塞一个随机串（如 `SENTINEL=7f3a91`），
   要求它在结果里复述。没有复述 = 消息没送达，不是「模型没听话」。
4. **负对照**：同一 harness 跑一个不该派生 subagent 的任务，确认没有 `spawn_agent`。
5. **超时即失败但保留现场**：不自动清理 `runs/<runId>/`，便于复盘。

### 4.3 场景矩阵

| 场景 | 提示词要点 | 断言的核心事实 |
| --- | --- | --- |
| S1 基础 | 「派一个子代理算 X，然后告诉我结果」 | `spawn → 完成 → read_agent_result` 全出现；结果含哨兵 |
| S2 并行 | 「同时派 3 个，分别算 A/B/C」 | 3 个 `spawn_agent` 在 1 个回合内；3 个终态事件；**无重复 spawn** |
| S3 超时 | 「派一个需要 120s 的任务，先别等」 | `wait` 超时后子代理仍 `running`；后续 `wait` 能拿到结果 |
| S4 steer | 「派一个统计任务，中途追加要求」 | `followup_task` 后子代理收到新指令（哨兵 2） |
| S5 team 基础 | 「建 3 个任务的团队，分给 2 个 worker」 | 任务板终态全 completed；无 zombie running |
| S6 team 规模 | 「10 个简单 + 3 个中等任务」 | 全部完成；不超时；面板行数 = 成员数（不静默截断） |
| S7 取消 | 「派 2 个，然后取消其中一个」 | 取消的终态 `stopped`；另一个不受影响；槽位释放 |
| S8 负对照 | 「今天几号」 | 无 `spawn_agent` / `spawn_teammate` |

### 4.4 判据分级（不许含糊）

| 级别 | 含义 | 例子 |
| --- | --- | --- |
| 构造得出 | 代码路径存在 | 「`close()` 会设置 stop 信号」 |
| 运行时发了 | 事件流里出现 | 「`subagent_end` 事件 status=completed」 |
| 用户看到了 | 前端渲染 | 「侧栏出现子代理卡片且状态是绿色」 |

**只有第三级才能写「UX 已修复」**；前两级只能写「机制已实现」。

---

## 5. 优化设计

### 5.1 `wait_agent` 不再阻塞回合（P1，对标 GA `4655f83`）

**问题**：现在 `wait_agent` 是工具调用，模型必须等它返回才能继续。
派 3 个活要串行等 3 次，期间母代理无法做任何事，前端看起来像卡死。

**设计**：`wait_agent` 默认行为改为「**最多等 `timeout_seconds` 秒，且最多等一个事件**」，
返回三种回执之一：

```json
{"state": "all_terminal", "results": {...}}
{"state": "event", "events": [...], "still_running": ["a","b"], "event_seq": 42}
{"state": "timeout", "still_running": ["a","b"], "event_seq": 42}
```

- 模型看到 `timeout` 时，可以去做别的事（继续本地工作），也可以再 wait；
- 关键：**不再有「这段时间什么都做不了」的语义**——超时回执本身就是可行动信息；
- 保留 `condition="all_terminal"` 让模型显式要求「必须等完」。

**兼容**：现有测试 `test_wait_timeout_does_not_kill_child` 继续通过。

### 5.2 steer：`followup_task`（P2，对标 GA `4655f83` / CC `SendMessage`）

**问题**：子代理在跑，母代理发现任务说明有问题，只能 `close_agent` 重派，
已完成的 80% 工作全部扔了。

**设计**：新增工具 `followup_task(target, message)`：

| 项 | 契约 |
| --- | --- |
| 目标状态 | 必须 `running`；终态返回明确错误（建议 `read_agent_result`） |
| 投递时机 | **不打断当前工具调用**，在本轮工具批次结束后、下一次 LLM 调用前注入 |
| 注入形式 | 作为 `user` 消息追加到子代理 history，前缀 `[来自父代理的追加指令]` |
| 幂等 | 同一条 message 重复提交按内容哈希去重 |
| 上限 | 每个子代理最多 5 条追加，超过返回错误 |
| 返回 | `{ok, delivered_to_turn: N, pending: bool}` |

**不做**：不打断正在执行的 `code_run`（会破坏子代理的原子操作）；
不注入到已完成子代理（那是 `resume_agent`，不在本轮范围）。

### 5.3 名字钉住（P2，对标 GA `name_pins.json` / CC `sendMessagePins`）

**问题**：子代理 `probe` 关闭后，新建一个也叫 `probe`，旧的消息可能投进新邮箱。

**设计**：

- 每个 `task_name` 第一次创建时登记 `{name → first_agent_id}`，写入会话级存储；
- 同名**新** agent 创建时自动加后缀 `_2`、`_3`（GA 的做法），并在 `list_agents` 里如实显示；
- `send_message` / `followup_task` 的参数若指向已关闭 agent 的名字，
  返回明确错误：`probe 已关闭（agent_id=sub:probe#1），请用 list_agents 确认目标`。

### 5.4 子代理完成通知的 fold 注入（P2）

**问题**：子代理完成时只往 SSE 发事件给前端，母代理的对话 history 里什么都没有。
母代理下次要拿结果，必须记得调 `read_agent_result`。

**设计**（对齐 CC `task-notification` / GA 2.2）：

- 子代理进入终态时，往**会话级通知队列**写一条：
  `{agent, status, result_chars, result_preview(400), event_seq}`；
- 母代理的 `AgentLoop` 在**每次 LLM 调用前**（fold 点）调用
  `notification_injector.pending()`：
  - 有未读通知 → 以 `system` 或 `user` 角色追加一条 `<task-notification>`
    （建议 `user`，与 CC 一致，避免破坏 system 缓存前缀）；
  - 注入后标记已读，但**不删除**（`peek/ack` 分离），失败可重放；
- 注入内容有预算：一次最多 5 条，每条预览 400 字符，超出写「另有 N 条，用 list_agents 查看」；
- **回合结束仍未被注入的通知保留**，下个用户回合继续注入（这正是 GA 修的 D5）。

**代价**：每次 LLM 调用前多一次内存查询，可忽略。

### 5.5 `notify_when_idle`（P2）

`notify_when_idle(target, mode)` 只是往通知队列注册偏好：
子代理从 `running` 转 `idle`（一轮结束但未关闭）时推一条通知。
本期只做「注册 + 推送」，不做复杂的策略。

### 5.6 关闭竞态与任务归还（P0.5，先测后修）

按 §3.1 / §3.4 的可疑点，用 S7 场景逼出来。修复方向（若复现）：

1. **终态写入只允许一次**：`mark_terminal()` 用 CAS（`if node.status in {starting,running}`）；
2. **关闭不覆盖已完成**：`close()` 遇到 `status=completed` 只释放槽位，不改状态；
3. **teammate 关闭必须归还任务**：关闭路径统一调 `reclaim`，失败要记日志但不得抛出；
4. **收尾幂等**：`acquire_lifecycle()` 已保证一次，但关闭事件要允许「已发过就跳过」。

### 5.7 事件持久化（P3，可选）

`event_seq` 现在是内存游标，进程重启归零。GA 用文件源 + 只读游标解决。
Hlaudz 可先做「事件表 + 自增 seq」，重启后 seq 继续增长，游标语义不变。

### 5.8 UX 优化

| 项 | 现状 | 目标 |
| --- | --- | --- |
| 子代理播报 | 可能先 failed 后 completed 两行并存（GA 修过） | 同一 agent 只保留最终状态一行 |
| team 面板行数 | 未知是否硬截断 | 超过 N 行显式写「另有 M 个」 |
| 关闭中的成员 | 未知显示什么 | 新增 `stopping` 状态，不显示为 running |
| 子代理耗时 | 未显示 | 每个成员显示已运行时长 |
| 失败原因 | 只有 error 字符串 | 面板可展开看到最后一条工具错误 |

---

## 6. 测试矩阵

### 6.1 L1 新增单测

`backend/tests/test_subagent_lifecycle.py`

| 用例 | 断言 |
| --- | --- |
| `test_close_after_complete_keeps_completed` | 先完成再 close → 状态仍 completed |
| `test_close_during_complete_is_idempotent` | 并发 close/完成 → 只有一个终态，无异常 |
| `test_drive_exception_sets_errored` | 子代理抛异常 → `errored` 且 error 非空 |
| `test_wait_cursor_is_not_destructive` | 两个等待者都能看到事件 |
| `test_wait_timeout_returns_actionable_receipt` | 回执含 `still_running` |
| `test_followup_delivers_at_fold_point` | 注入在工具批次之后 |
| `test_followup_rejected_on_terminal` | 终态返回明确错误 |
| `test_followup_dedup_by_hash` | 同内容只投一次 |
| `test_name_pin_suffix_on_reuse` | 同名新建得到 `_2` |
| `test_send_message_to_closed_name_is_error` | 明确错误，不静默投递 |
| `test_notification_peek_ack_separable` | peek 不删、ack 才标记 |
| `test_notification_survives_turn_end` | 回合结束仍可注入 |
| `test_teammate_close_reclaims_task` | 关闭后任务回到 pending |
| `test_event_seq_survives_restart` | 重启后 seq 继续增长（P3） |

### 6.2 L2 集成（假 LLM 脚本化）

| 用例 | 断言 |
| --- | --- |
| `test_parallel_spawn_three_no_duplicate` | 3 个 spawn，1 个回合内，无重复 |
| `test_team_scale_13_tasks_all_complete` | 13 个任务全部 completed |
| `test_cancel_one_does_not_affect_other` | 取消一个，另一个正常完成 |
| `test_notification_injected_before_next_llm_call` | 假 LLM 的入参里有 `<task-notification>` |

### 6.3 L3 真实 e2e

见 §4.3 的 S1–S8。每次运行必须落 `runs/<runId>/`，并把结论回填到本文 §8。

---

## 7. 实施阶段

| 阶段 | 内容 | 交付 | 预估 |
| --- | --- | --- | --- |
| A | 建 L3 骨架 + S1/S2/S8 | e2e 可跑，跑出第一批缺陷 | 1 天 |
| B | 逐缺陷 TDD（含反向确认） | §6.1 对应用例绿 | 1-2 天 |
| C | S5/S6/S7 team 场景 | team 真实链路绿 | 1 天 |
| D | `wait` 非阻塞 + steer + 名字钉住 | 新工具可用 + 测试 | 1 天 |
| E | 通知 fold 注入 + notify_when_idle | 通知不丢 | 1 天 |
| F | UX 五项 + 真实前端验证 | 截图 + 运行时事件 | 0.5 天 |

**顺序不可颠倒**：A 必须先于 B；先建观测再修，否则修了什么、有没有修好都说不清。

---

## 8. 真实运行记录（实施时回填）

| runId | 场景 | 结果 | 暴露的缺陷 | 处理 |
| --- | --- | --- | --- | --- |
| （待填） | | | | |

---

## 9. 验收标准

1. S1–S8 全部可重复通过（连跑 3 次，无 flake）；
2. 每个修过的缺陷都有一个「stash 实现后变红」的用例；
3. `list_agents` / team 面板在完成后 10 秒内不再出现 `running`；
4. 子代理结果必须带哨兵 token 才算送达；
5. 本文 §8 有至少 3 条真实 runId 记录。

---

## 10. 明确不做

- 不做跨进程子代理（Hlaudz 是单机 asyncio，不需要 IPC 总线）；
- 不做 workflow 脚本引擎；
- 不做「子代理之间自由对话」（team 已限制为只向 lead 汇报，这是有意的）；
- 不改 A2A 侧的任务模型（A2A task 与 team task 是两套，本轮不合并）。
