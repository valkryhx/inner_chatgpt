# Agent Team 加固：协作从「靠模型自觉」变成「平台硬保证」

> 日期：2026-11-11　状态：已实施并真实验证
> 起因：用户反馈"Hlaudz 的 agent team 运行得不太对劲"，并要求参考 CC319 与 GenericAgent
> 的成熟设计，让 agent 之间有**硬制度**保证消息传递，而不是靠 agent 自己传参数。
> 关联：`docs/20261106-AgentTeam-Web多用户隔离设计方案.md`（P3 原始设计）

## 0. 结论

用真实湛卢 LLM 打了 11 个场景后，抓到 **3 个真实缺陷**（全部是协作硬制度缺失，不是
模型不听话），逐条修复并在真机上复验：

| # | 缺陷 | 严重性 | 根因 | 修法 |
|---|---|---|---|---|
| D1 | `blocked_by` 依赖只在 `claim` 路径生效，`spawn_teammate` 绑定路径完全不检查 | **高**：依赖形同注释 | 两条分配路径各写各的，只有一条做了校验 | 抽出 `task_is_blocked()`，两条路径共用；且在 **spawn 之前**就拒绝 |
| D2 | 同名 teammate 复用后，任务 owner 绑到旧名字，活着的 agent 无法完成自己的任务 | **高**：任务只能由 lead 代收 | `spawn_teammate` 先按**请求的名字**绑定 owner，再 spawn；而 spawn 会给重名加后缀 | 改为**先 spawn，拿真实 `agent_id` 再绑定**；被拒时关闭刚拉起的 agent |
| D3 | lead 用裸名（`poet`）发消息，teammate 的收件箱是 `sub:poet`，消息永久读不到 | **高**：真正的"消息传不过去" | 写路径与读路径各自拼名字，没有统一归一化 | `normalize_agent_id()` + `team_name_pins` 名字钉住表 |

外加一条**能力缺口**（不是 bug，是设计缺失）：

| # | 缺口 | 后果 |
|---|---|---|
| D4 | `send_message` 只写数据库，**从不投递** | 消息到了库里，但只有模型主动 `list_messages` 才看得到。真机上 teammate 两次读自己的收件箱都是空，45 秒窗口足够也没用——**"协作"实际依赖模型习惯主动轮询** |

## 1. 参考的两套实现（先看懂了再动手）

### 1.1 CC319（`claudecode283_hack/docs/20261108-CC319-agent-team实现机制调研.md`）

关键结论：**邮箱是文件，投递是平台行为**。

- 每个 agent 一个收件箱：`<configDir>/teams/<team>/inboxes/<agent>.json`；
- 写邮箱 `YE()` 有**五道闸**：团队已删 / schema 非法 / 无磁盘 / 未知错误 / 原子写；
- 读后用 `Sze()` **直接剪掉**已投递条目（`pruned`），避免无限增长；
- **至少一次投递**：`markMessagesAsRead` 失败就重投，连续 5 次失败才「不标记直接投递」；
- **名字钉住** `sendMessagePins`（`displayName → agentId`）：名字可被新 agent 复用，
  一旦 pin 过，发往该名字的消息优先投给 pin 的 agent；
- 消息到达模型时包成 `teammate_mailbox` attachment → `<system-reminder>`，
  `isMeta: true` 让它显示成系统提示而非用户发言；
- **`in-process` 运行时是 500ms 轮询**：① 用户插话 ② 邮箱 ③ 任务板可认领项；
  连续 30s 无事 → `TeammateIdle` 钩子 → 退出。

CC319 §17.1「必须」清单里，与本项目相关的四条：**每 agent 一个可持久化收件箱**、
**至少一次投递（标记成功才删）**、**按名字寻址 + name→id 的 pin**、**扁平 roster**。

### 1.2 GenericAgent（`genericagent/agents/mailbox.py` + `manager.py`）

GA 把 CC 的思路落成了 Python，几处比 CC 更适合本项目：

- `SubagentMailbox`：`mailbox.jsonl` + `cross_process_lock` + 原子整文件重写；
- 每行带 `state`：`queued → delivered → consumed`，非正常路径 `held/expired/dropped/forged`；
- `delivery_mode` 分 `QUEUE_ONLY` 与 `TRIGGER_TURN`——后者**会唤醒对方进入新一轮**；
- **`_reject_dead_mailbox()`**：发给已结束的 agent 直接拒绝并给出「该怎么做」，
  而不是静默写入一个没人读的文件（GA 注释里明确写了这是他们在真机上踩过的坑：
  "the sender got a success receipt -- the message silently vanished"）；
- **`_check_name_pin()`**：与死信箱检查分开，"is anyone listening" 与 "is it the same
  listener as before" 是两个问题，两者都要问；
- lead 没有自己的 mailbox 文件，它的收件箱是**共享 parent inbox**，与 wait/通知流同源。

## 2. 本项目改了什么

原则：**不引入新进程、新守护线程、新文件总线**。Hlaudz 是 Web 多用户 + SQLite 架构，
把 CC 的"文件邮箱 + 500ms 轮询"照搬会多出一个轮询器和一套文件锁。等价能力用
「SQLite 表 + 每轮模型调用前的注入点」实现，语义一致而成本更低。

### 2.1 收件人归一化（治 D3）

`team_store.normalize_agent_id()`：

```
lead / root / main / team-lead  ->  "root"
poet / sub:poet / /root/poet    ->  "sub:poet"
```

**写和读走同一个函数**：`send_message` 归一化收件人，`list_messages` 归一化查询参数。
这是 D3 的正面修法——之前写路径存 `poet`、读路径查 `sub:poet`，两个篮子。

### 2.2 名字钉住（治串名，参考 CC319 `sendMessagePins`）

新表 `team_name_pins(team_id, name, agent_id, pinned_at)`：

- `pin_name()` 第一次绑定；之后同名绑到**不同** agent 直接抛错，不静默改投；
- `resolve_name()` 优先返回 pin，未 pin 则默认 `sub:<name>`；
- `spawn_teammate` 成功后自动 pin（用真实 agent_id）。

### 2.3 投递状态机（治 D4）

`team_messages` 新增 `state` 列：`queued → delivered → consumed`。

- `send_message` 写入时 `state='queued'`；
- 投递成功（对方活着，消息进了它的待处理队列）→ `mark_messages_delivered()` → `delivered`；
- 被消费（注入到模型上下文）→ `consume_inbox()` 在同一事务里标记 `consumed` + `read_at`。

**`consume_inbox()` 是投递步骤，不是读取步骤**：它返回的行马上就要进模型，所以在同一个
事务里标记。这正是 at-most-once 语义（与 CC319 剪掉已投递条目等价）。旧实现只能"读"，
读多少遍都还在，所以无法区分"没送达"和"送达了没人看"。

### 2.4 硬投递链路（治 D4 的主体）

```
lead 调 send_message
  → TeamHost.send_message 归一化 + 入库 + 调 _deliver()
  → 注入的 deliver_sink（由 turn runtime 提供）
      ├ 收件人是 lead 且发件人不是 lead → host.push_team_message()   （进 lead 通知队列）
      └ 其他                          → host.deliver_team_message()  （进该子代理 pending_messages）
```

反过来，**teammate 发给 lead** 时：

```
teammate 调 send_message
  → teammate 的 TeamHost（由 SubagentRuntime 装配，带自己的 deliver_sink）
  → runtime.lead_delivery_sink = 父 host.push_team_message
  → lead 的通知队列
```

**注入点只有一个**：`AgentLoop.run_stream` 每轮模型调用前调 `message_injector`。
lead 的注入器（`session.py::_lead_injector`）每轮做两件事：

1. `team_store.consume_inbox()` —— 从库里消费积压的收件箱（发件时对方恰好不在活体队列的情况）；
2. `host.drain_notifications()` —— 内存里的子代理结束通知。

子代理的注入器是 `node.drain_pending()`，投递进去的消息和 `followup_task` 走同一条队列，
所以**steer 与团队消息行为一致**。

### 2.5 依赖硬约束（治 D1）

抽出 `team_store.task_is_blocked()`，让两条分配路径共用：

- `claim_task()`（原有）；
- `bind_task_owner()`（**新增校验**，D1 的根因就在这里）；
- `spawn_teammate()` **在 spawn 之前**先查一次——避免"拉起一个马上要关掉的 agent"。

### 2.6 spawn 顺序反转（治 D2）

旧：绑定 owner（用请求名）→ spawn（可能被加后缀）→ owner 与真实 agent 对不上。
新：**spawn → 拿 `spawned.agent_id` → 绑定 → pin**。绑定被拒时关闭刚拉起的 agent，
不留孤儿占槽位。`owner_agent_id` 仍作为**提示**传给 spawn_fn（既有测试依赖这一点），
但**权威 owner 永远是 spawn 的返回值**。

### 2.7 可观测性

- `send_message` 返回体新增 `live`（对方此刻是否活着）与 `delivery_note`（没送达时说明原因）；
- 新增 SSE 事件 `message_injected`，前端渲染成可折叠的「团队消息」行——
  否则用户只看到模型行为突变，不知道为什么；
- 新增 `injected_message` block 类型，刷新后仍在。

`chat.py` 是 `elif` 白名单，不写分支就会静默丢事件——这是 skill §8 记录过的坑，
本次专门按该条验证（断言落在 SSE 帧上，不仅测 `publish()`）。

## 3. 测试

### 3.1 单测：488 → 505（新增 17）

`backend/tests/test_team_hard_contract.py`，每条对应一个缺陷：

| 组 | 项 | 覆盖 |
|---|---|---|
| D1 依赖 | 3 | bind 拒绝 / 正向对照（依赖完成后允许）/ spawn 前拒绝 |
| D2 owner | 2 | 绑定真实 agent_id / 真实 agent 能完成自己的任务 |
| D3 归一化 | 3 | 裸名可达 / 两种写法同一收件箱 / 四个 lead 别名 |
| D4 投递 | 2 | 状态机推进 / 收件箱只返回自己的 |
| 名字钉住 | 2 | 拒绝改绑 / 默认 `sub:` 前缀 |
| 宿主投递 | 3 | 活体投递 / 死 agent 拒绝 / lead 收到队友消息 |
| 其他 | 2 | 未知收件人标 `live=false` / teammate 不能互发 |

### 3.2 反向确认（确认测试真的在测东西）

逐个去掉实现，确认对应测试变红：

| 去掉什么 | 结果 |
|---|---|
| `bind_task_owner` 的依赖检查 | **RED** |
| 名字钉住的改绑拦截 | **RED** |
| `consume_inbox` 标记 consumed | **RED** |
| 死 agent 拒绝投递 | **RED** |
| owner 改回按请求名绑定 | **RED** |
| 收件人归一化（store 层） | GREEN ——**不是假绿**：`resolve_name` 也做了归一化，两层防护。两处同时去掉即 RED（已实测） |

### 3.3 真实 e2e（真实湛卢 `moma/deepseek-latest-flash`）

11 个场景，全部真 LLM + 真 SSE + DB 双记录比对：

| 场景 | 内容 | 结果 |
|---|---|---|
| T1 | 2 任务 1 队友 | 8/8 PASS |
| T2 | 2 队友各自 `send_message` 回传 lead | 12/12 PASS |
| T3 | `blocked_by` 依赖顺序 | **修复前 FAIL**（`b_start_seq=5` 早于 `a_done_seq=6`）→ 修复后 PASS |
| T4 | 跨轮恢复任务板 | 8/8 PASS（第二轮 3 个任务全由 teammate 完成） |
| T5 | 队友→lead 自动送达（禁止 lead 轮询） | 3/3 PASS |
| T6 | lead→运行中队友推消息 | 修复前消息 `delivered=0` 收不到 → 修复后可达 |
| T7 | 同名 teammate 复用 | **修复前** owner 绑到 `sub:worker`，活着的 `worker_2` 无法 complete，lead 代收 → **修复后** owner 正确是 `sub:worker_2` |
| T8 | 依赖绕过交叉验证 | 修复前模型自己报告"spawn 不设防" → 修复后返回 `{"ok":false,"reason":"blocked"}` |
| T9 | 复杂交接：A 产出 N → B 基于 N 计算 | 45.6s，N=76、3N=228，双方交叉核对一致 |
| T10 | 停止级联 | 5/5 PASS（团队 stopped / 任务回 pending / 子代理 stopped / 消息 stopped） |
| T11 | 运行中投递 | **修复前** MID_SENTINEL 没出现 → **修复后** 队友下一轮看到了并执行 |

### 3.4 决定性证据（T5/T11）

修复后模型的原话：

> "我是从**自动送达的团队消息**里看到的，不是靠调用 `list_messages`：`wait_agent` 返回
> `state=all_terminal` 之后，我下一条消息的上下文里直接出现了
> `[来自 calc_probe 的团队消息] 7*13=91`"

T11 中 lead 发给队友的消息 `state=delivered`，队友在下一轮模型调用前看到
MID_SENTINEL 并据此调整了行为；队友发给 lead 的消息 `state=consumed`，
lead 全程未调用 `list_messages`。

### 3.5 UI（Playwright，真实浏览器）

`work/_inj_ui_20261111_TR1.py`：6/6 PASS。注入消息在面板渲染成可折叠的「团队消息」行，
SSE `message_injected` 2 条、UI 文本含「团队消息」、无 console 报错。
现场 `work/team_ui/inj_082411-502b/injected.png`。

## 4. 一个尚未实现的取舍（诚实记录）

CC319 的 in-process teammate 在**空闲时"醒着"**：500ms 轮询邮箱和任务板，有活就干，
30s 无事才退出；GA 用 `TeammateIdle` 钩子继承了这一点（"A teammate never idles while
work is claimable"）。

Hlaudz 目前是**一次执行**模型：teammate 完成手头任务就结束。因此：

- **已支持**：消息在对方运行期间投递（下一轮模型调用前生效）；
- **未支持**：对方已结束后还能"叫醒"它继续干新活。发给已结束 agent 的消息现在会
  **明确返回 `live=false` + 原因**（而不是静默消失），要它继续工作需重新 `spawn_teammate`。

这不是 bug，是"持久化 worker"与"一次性 subagent"的产品差异。要做的话需要引入
idle-poll 循环 + 唤醒语义，属于独立阶段。

## 5. 变更文件

| 文件 | 改动 |
|---|---|
| `backend/app/team_store.py` | 收件人归一化 / `state` 列（含旧库迁移）/ `consume_inbox` / `mark_messages_delivered` / `pin_name` / `resolve_name` / `task_is_blocked` / `bind_task_owner` 加依赖校验 |
| `backend/app/team_host.py` | `deliver_sink` 注入 / `send_message` 归一化+投递+`live` 标记 / `consume_inbox` / `pin_name` / `resolve_name` / `spawn_teammate` 顺序反转+预检+真实 owner+pin |
| `backend/app/subagent/host.py` | `deliver_team_message` / `push_team_message` / `deliver_pending_inbox` / `_sender_label` / 子代理启动时补投积压 |
| `backend/app/subagent/session.py` | lead 的 `_deliver` 路由 / 异步 `_lead_injector`（收件箱+通知）/ `_close_teammate` |
| `backend/app/subagent/runtime.py` | teammate 的 TeamHost 带 `deliver_sink` / `lead_delivery_sink` |
| `backend/app/subagent/registry.py` | 记录 `requested_name` |
| `backend/app/subagent/prompts.py` | 依赖硬约束 / 起名一次性 / 消息由平台投递 / 收到 `[来自 X 的团队消息]` 如何处理 |
| `backend/app/team_tools.py` | `send_message` 描述重写（投递语义 + 两种名字写法 + `live` 含义） |
| `backend/app/agent_loop.py` | 注入器支持 awaitable（读库的收件箱需要 await） |
| `backend/app/routers/chat.py` | SSE 新增 `message_injected` 分支 + `injected_message` block |
| `frontend/src/types.ts` | `InjectedMessageBlock` + `message_injected` 事件 |
| `frontend/src/App.tsx` | 处理 `message_injected` |
| `frontend/src/components/MessageList.tsx` | `InjectedNote` 可折叠组件 |
| `frontend/src/styles/index.css` | `.injected-note*` 样式 |
| `backend/tests/test_team_hard_contract.py` | **新增** 17 项 |
| `backend/tests/test_team_host.py` | 适配 spawn 顺序反转（不变量不变：无幽灵 owner） |

## 6. 验证命令

```bash
cd backend && python -m pytest tests -q          # 505 passed
cd frontend && npm run build                      # 通过

# 真实 e2e（需后端在 127.0.0.1:8000 且配好湛卢）
python work/_team_e2e_20261111_TR1.py T1,T2,T3,T4
python work/_team_e2e2_20261111_TR1.py T7,T8
python work/_team_e2e3_20261111_TR1.py T9,T11
python work/_team_msg_e2e_20261111_TR1.py T5,T6
python work/_team_stop_20261111_TR1.py T10
python work/_inj_ui_20261111_TR1.py               # UI
```
