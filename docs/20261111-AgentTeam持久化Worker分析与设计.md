# Agent Team 演进为持久化 Worker：分析与设计

> 日期：2026-11-11　状态：**P-A 已实施并真实验证**（P-B/P-C 未做）
> 复核：2026-11-11 第二轮，逐条对照代码复核，发现 **7 处逻辑/实现漏洞**（见 §10），
> 本文 §3 的方案已按复核结论修订。
> 关联：`docs/20261111-AgentTeam协作硬制度加固.md`（刚完成的 D1–D4 加固）
> 参考：CC 2.1.319 `docs/20261108-CC319-agent-team实现机制调研.md`（§10.6/§10.8/§16.4）、
> GenericAgent `genericagent/hooks/`（1939 行）+ `docs/20261014-ga-agent-team-复刻实施方案.md`

## 0. 结论先行（四句话）

1. **值得做，但"已经铺好 80%"这个初判是乐观的。** 复核后发现循环骨架只占工作量的小头；
   真正麻烦的是**现有代码里大量逻辑以「子代理必然进终态」为前提**，持久化会同时打破
   至少 7 处（§10）。上一轮加的 injector / consume_inbox / deliver_sink 确实是必要的
   零件，但**不足够**。
2. **`TeammateIdle` 钩子不是持久化的前置条件。** CC319 里 idle 的本质是轮询做三件事
   （用户插话 / 读邮箱 / 认领任务），钩子只是**在空闲这一刻给外部一个插手机会**。
   没有钩子也能做持久 worker。
3. **建议分三步**：P-A 持久循环骨架 → P-B 自动认领 → P-C idle 通知帧。
   钩子单独立项。**P-A 的验收标准必须包含 §10 的 7 条回归**，否则会改出比现在更糟的行为。
4. **最危险的一处是 `wait_agent`**：它按 `is_terminal` 判定"全结束"，队友持久化后
   永远不终态，lead 的 `wait_agent(condition=all_terminal)` 会挂到超时。
   这是 P-A 必须第一个解决的问题（§10.1）。

---

## 1. 现状坐标：Hlaudz 差在哪一步

### 1.1 三方对比

| 维度 | CC 2.1.319 | GenericAgent | Hlaudz 现状 |
|---|---|---|---|
| teammate 生命周期 | in-process，**跨多轮存活**，30s 空闲才退 | 子进程，`reply.txt` 收工后进 **wait-reply 轮询窗口** | **一次执行**：`run_stream` 返回 `done` 即终态 |
| 唤醒源 | ① 用户插话 ② 邮箱 ③ 任务板可认领 | ① 邮箱（`consume_trigger_turn`）② 任务板（`_teammate_idle_hook`） | **无**（只有 spawn 时那一条 user message） |
| 空闲语义 | `TeammateIdle` 钩子，可 `blockingError` 阻止退出 | `TeammateIdle` 钩子同语义 | 结束即 `completed` |
| 空闲时长 | 23–30s（`IIt=30000`） | `idle_timeout` 参数 | 无 |
| 空闲也"醒着" | 是 | 是 | **否** |
| 自动认领任务 | `Le()`：pending + 无 owner + 无 blockedBy | `_teammate_idle_hook` 同逻辑 | **无** |
| 生命周期终止回收 | `m0300 N4e`：owner 释放回 pending | `reclaim_tasks_for()` | 有（close/stop 时） |

Hlaudz 现在的队友 = **一次性 RPC**：派活 → 跑完 → 结束。上一轮加固解决的是
「运行期间消息能不能到」，这一轮要解决的是「跑完之后还能不能继续干活」。

### 1.2 用户实际会遇到的差别

```
用户：先让 alpha 统计文件数，我再决定下一步。

现在：alpha 报完 N=76 就结束了。用户下一轮说"再算个平均大小" →
      只能重新 spawn_teammate（新的 agent、新的上下文、重新自我介绍）。
      更糟：上一轮 alpha 已经沉淀的上下文（它读过哪些目录、遇到过什么坑）全丢了。

CC/GA：alpha 报告后进入 idle 等待；用户下一轮说"再算个平均大小" →
      消息投递给同一个 alpha，它带着全部上下文接着干。
```

这就是「持久化 worker」的真实价值：**不是省一次 spawn，是保住上下文**。

---

## 2. 关键设计问题：Hlaudz 的哪些约束决定了方案

### 2.1 约束一：一个 SSE = 一次生成 = 一个 turn

`chat.py::_event_stream` 的生命周期绑死了一次用户消息。**两轮之间 SSE 是关掉的**。
这意味着队友不可能"横跨两次用户消息活着"还保持事件可见——除非改变 SSE 生命周期。

这条约束直接否掉「和 CC 完全一样的常驻进程内 agent」：CC 的 terminal 是常驻的，
Hlaudz 的 HTTP 请求不是。

### 2.2 约束二：`_merged_events` 已经在等子代理

```python
# chat.py
if main_done:
    if not has_active():   # has_active = host.has_active() or acp.has_active()
        break
    # 否则保持 SSE 打开，最多 TEAM_DRAIN_TIMEOUT_S（默认 300s）
```

**这是一个现成的持久化窗口**：主代理说完话后，SSE 会继续给活着的子代理最长 300 秒。
持久 worker 的"空闲期"应该活在这个窗口里，而不是跨轮。

### 2.3 约束三：Web 多用户 → 不能有常驻后台线程

每用户每会话一个常驻轮询器，在 Web 多用户下会退化成 N 个空转协程。CC 是单用户
terminal，GA 是单进程 CLI，都没有这个问题。

### 2.4 由此推出的定位

> **Hlaudz 的持久 worker = 在「本轮 SSE 存续期内」持续存活的具名 worker，
> 空闲时有唤醒源、空闲超时才退；跨用户消息轮次仍靠 `spawn` 重建（但状态在 DB）。**

这不是"缩水版"，而是 Web 形态下的正确语义：轮的边界是产品事实，不该假装不存在。

---

## 3. 方案设计

### 3.1 核心改动：把 `_run_child` 从「跑一次」改成「跑一轮，等，再跑一轮」

`backend/app/subagent/host.py::_run_child` 当前是 117 行（139–256），结构是：

```
build runtime → run_stream → 收尾（写库、通知、publish）
```

改成：

```
build runtime
loop:
    ① 处理积压注入（现有 consume_inbox + drain_pending）
    ② 没有可干的 → 进入 idle wait（≤ idle_timeout）
         唤醒源：新消息 / 新可认领任务 / stop / 关闭
    ③ 有活 → 自动认领（若有）→ run_stream 一轮 → 不结束
        - 模型明确说"我做完了"（无 tool_calls）→ 判为「本轮完成」
    ④ 若 idle 超时且无可认领任务 → 退出（现状行为）
```

关键：**`run_stream` 结束 ≠ agent 结束**。现在的 `done` 直接落到 `node.status = "completed"`，
要改成「一轮结束」，只有 idle 超时/close/stop 才进终态。

### 3.2 三轮循环需要的新状态

`AgentNode` 加：

| 字段 | 用途 |
|---|---|
| `idle_since: float` | 最后一次有进展的时刻，用于 idle 超时 |
| `wake: asyncio.Event` | 唤醒源（消息到达 / 任务可认领 / stop） |
| `rounds: int` | 跑了多少轮（现有 `turns` 是模型调用次数，两者不同） |
| `idle_reason: str` | 为什么进入空闲（available / blocked / done），用于通知帧 |

`SubagentHost` 加：

| 方法 | 用途 |
|---|---|
| `wake_agent(name, reason)` | 投递消息/新任务后唤醒 |
| `_claimable_task()` | 查任务板有没有 pending + 无 owner + 无 blockedBy |
| `_idle_wait(node, timeout)` | 等 wake / stop / 超时，返回进入哪条分支 |

### 3.3 自动认领（参考 CC `Le()` / GA `_teammate_idle_hook`）

已有零件：`team_store.list_tasks()` + `_deps_complete()`。缺一个"找第一个可认领"的查询：

```python
async def find_claimable(scope) -> dict | None:
    """pending + 无 owner + 所有 blocked_by 已 completed，按 created_at 最早优先。"""
```

认领成功后，把 CC 的 prompt 模板喂进去（**照抄 CC 的措辞**，它经过大量调优）：

```
Complete all open tasks. Start with task #<id>:

<subject>

<description>
```

**必须做的取舍**：Hlaudz 的 teammate 现在只在 `spawn_teammate` 绑定时才有任务。
开自动认领后，**lead 建了任务但没派活**的，队友会自己去抢。这是个行为变化，
需要提示词明确写清，并且**默认关闭**（env 开关），避免破坏现有编排习惯。

### 3.4 idle 通知帧（参考 CC `m0300 mat` / GA `build_idle_notification`）

队友进入空闲时给 lead 发结构化一帧，而不是只有最终 result：

```json
{"type": "idle_notification", "from": "alpha", "idle_reason": "available",
 "summary": "idle", "completed_task_id": "task_x", "completed_status": "completed"}
```

CC 的两个细节值得照抄：

1. **显式 DM 去重**（`m1448 zo`）：若队友本轮已经 `send_message` 给 lead，
   就**抑制**这条 idle 帧，避免"我发过了又来一条一样的"。
   GA 已实现：`_sent_to_lead_this_round()`。
2. **结果长度预算**：单条 ≤ 4000 字符，一次 drain 总预算 16000，超出截断并附
   「ask the agent for the rest via send_message」。

### 3.5 唤醒源的物理实现（Web 形态）

不能开轮询线程，所以用 `asyncio.Event` + 三个触发点：

| 触发点 | 在哪 | 做什么 |
|---|---|---|
| 消息投递 | `SubagentHost.deliver_team_message()` | `node.wake.set()` |
| 任务创建/回收 | `TeamHost.create_task()` / `reclaim()` 之后 | 遍历活体节点 `wake.set()` |
| 用户停止 | `stop_all_now()` | 现有 `stop.set()`（已覆盖） |

外加一个**兜底轮询**：`_idle_wait` 内部用 `asyncio.wait_for(wake.wait(), timeout=min(2.0, remaining))`，
超时后主动查一次任务板。这样即使某个触发点漏了，最坏延迟 2 秒——比"漏了就永久卡住"安全。

> cc: 这个兜底不是懒，是**容错设计**。CC 的 500ms 轮询本身就是兜底；
> 我们用事件为主 + 轮询兜底，延迟更低但保留同样的鲁棒性。

### 3.6 与 SSE 生命周期的衔接

`_merged_events` 的 `has_active()` 现在决定"主代理说完后还要不要等"。持久 worker
会让它**更长时间保持 true**。这里必须加约束，否则一个 idle 的队友能把 SSE 拖到
`TEAM_DRAIN_TIMEOUT_S`（300s）：

| 场景 | 期望 |
|---|---|
| 队友在跑 | 继续等（现状） |
| 队友 idle 且在等可认领任务 | **加入 idle 预算**：最多等到 `TEAMMATE_IDLE_TIMEOUT_S`（默认 30s），之后视同结束 |
| 队友 idle 且任务板已空 | **立即结束**（没什么可等的） |

实现上给 `has_active()` 加一个 `has_pending_work()` 子条件：
`idle 且无可认领任务` 不算 active。这是本轮改动里**最容易出错、必须有测试**的一处。

---

## 4. 钩子：要不要做、要做什么

### 4.1 CC319 的钩子到底给 team 提供了什么

33 个事件里与 team 相关的只有 4 个：

| 事件 | 作用 | 对持久 worker 的必要性 |
|---|---|---|
| `TeammateIdle` | 队友空闲时触发；**可返回 `blockingError` 阻止它歇** | 中（"外部塞活"能力） |
| `TaskCreated` | 任务创建时触发 | 低（内部已有事件流） |
| `TaskCompleted` | 任务完成时触发 | 低 |
| `SubagentStart` | 子代理启动 | 低 |

**关键判断：`TeammateIdle` 不是持久化的前提。** CC 的 idle 循环自己就会查任务板
（`Le()`），钩子只是"在查之前让外部插一句"。没有钩子，自动认领照样工作。

### 4.2 但现在有一个只有钩子能做的事

**`blockingError` 能阻止队友退出。** 场景：

```
队友跑完手头任务，进入 idle。任务板此刻是空的，按规则 30s 后退出。
但用户马上要说"再算个平均大小"——如果队友已经退出，就得重新 spawn。
```

钩子可以在这里判断「用户上一轮还在打字 / 有未提交的输入 / 最近 60s 内有用户活动」，
返回 `blockingError` 让队友多等一会儿。这是纯内部逻辑做不到的。

不过——**在内置逻辑里做同样的事更简单**：`_idle_wait` 里加一条
「最近 N 秒内本会话有用户活动（SSE 活跃 / 最近一条消息时间）就延长 idle」。
不需要引入通用钩子框架。

### 4.3 做钩子的真实成本（为什么建议拆开做）

| 成本项 | 说明 |
|---|---|
| 配置面 | `settings.json` 结构、事件匹配器、handler 五类（command/prompt/agent/http/mcp_tool）、超时、`onFailure` 语义 |
| 权限语义 | `GUARD_EVENTS` 失败即 block（fail-closed），`PreToolUse` / `PermissionRequest` 尤其危险——配错就锁死所有工具调用 |
| 输出协议 | `hookSpecificOutput.hookEventName` 必须匹配、`permissionDecision` 三态、`additionalContext` |
| 安全 | command handler 要跑子进程；Web 多用户下**谁有权配钩子**是一个权限模型问题 |
| 33 个事件里哪些真生效 | CC 自己的文档都有 `contract-only` 与 `integrated` 之分（GA `runtime.py:326` 明确标注 `TeammateIdle: integrated`，其余多数 `contract-only`） |

> **一句话**：钩子是「平台能力」，team 是「协作功能」。让 team 依赖钩子，等于让
> 一个功能态去驱动一个平台态，方向反了。**先做 team 的持久化，钩子作为独立阶段**。

### 4.4 如果将来做钩子，建议的最小面

不做 33 个，只做**真有人用**的 6 个，按优先级：

| 事件 | 为什么 |
|---|---|
| `PreToolUse` / `PostToolUse` | 唯一有强需求的（格式化、审计、拦截危险命令） |
| `TeammateIdle` / `TaskCompleted` | team 场景的扩展点 |
| `UserPromptSubmit` | 输入改写（消歧、加前缀） |
| `Stop` | 收尾动作 |

配置面沿用 CC 的 `hooks` 结构（生态兼容），handler 先只做 `command`（覆盖 90% 用例），
`onFailure` 默认 `continue` 而不是 CC 的 fail-closed——**Web 多用户下配错钩子不该让
所有人的工具调用挂掉**，这是本项目与 CC 单用户场景的关键差异。

---

## 4.5 复核：7 处逻辑/实现漏洞（必读，P-A 的验收清单）

第二轮逐条对照代码复核初版方案，发现初判过于乐观。**每一条都是"持久化会打破现有时序
假设"的具体表现**，不是风格问题。

### 漏洞 1（阻塞级）：`wait_agent` 会永久挂起

```python
# host.py:471
def _condition_met(self, nodes, condition):
    return all(n.is_terminal for n in nodes)   # ← 持久化后永不成立
```

- `wait_agent` 默认 `condition=any_event`（有进展就返回），**不受影响**；
- 但 `condition=all_terminal` 会一直等到 `timeout_s`（默认 60s）才返回 `state=timeout`；
- 提示词里明确写了「确实需要一次性等完时，才显式传 `condition=all_terminal`」，
  **模型真的会这么做**（S3/S10 场景就是这么写的）。

**后果**：lead 每调一次 `all_terminal` 白等 60 秒，然后拿到 `timeout` 而不是结果。

**修法**：`is_terminal` 的含义要拆成两个概念：

| 概念 | 含义 | 用于 |
|---|---|---|
| `is_terminal` | 生命周期结束（关闭/停止/出错） | 槽位回收、事件发布、SSE 关闭判定 |
| `is_idle` | **本轮工作完成**（无待处理消息、无可认领任务） | `wait_agent(all_terminal)` 的判定 |

即把 `_condition_met` 改成 `all(n.is_terminal or n.is_idle for n in nodes)`，
并在 `_wait_result` 里区分 `remaining` 与 `idle`，让 `state` 多一个
`ready`（有 idle 但没终态）的中间态。

> **这是 P-A 的第一个改动，必须先做。** 否则持久化一上线，lead 的等待语义立刻坏掉。

### 漏洞 2（阻塞级）：`read_agent_result` 拒绝读 idle 队友

```python
# host.py:546
if not node.is_terminal:
    return {"ok": False, "error": f"子代理仍在运行（{node.status}），请先 wait_agent"}
```

持久化后，队友"跑完一轮在等下一句"时 `is_terminal=False`，`read_agent_result` 直接拒绝。
而 lead 此刻**正需要**它上一轮的结论。

**修法**：允许在 `is_idle` 时读取，返回里加 `partial: true` 与
`note: "该 agent 仍在等待后续任务，这是它最近一轮的结论"`。
同时把 `node.result` 在每一轮结束时更新（现在只在 `finally` 里写一次）。

### 漏洞 3（阻塞级）：`subagent_runs` 一行一次 run，多轮无法记录

```python
# store.py:99  create_run  → INSERT 一次
# store.py:144 finish_run  → UPDATE 一次（status/result/turns/error/finished_at）
```

多轮循环若不改这里，DB 里永远是"第一次跑完时的状态"，
刷新页面看到的与真实状态不一致——这正是本轮 D1–D4 之前踩过的同类问题。

**修法**（二选一，建议 B）：
- A：每轮 `create_run` 一个新 `run_id` → 破坏 `read_result` 的 `get_run(node.run_id)` 与
  sidechain 的 `run_id` 关联，改动面大；
- B：**保留一个 run_id，新增 `rounds` 字段**，`finish_run` 只在真正终态时调用；
  每轮结束用新的 `update_run_progress(run_id, turns=..., result_text=...)` 更新快照。
  表结构与既有查询兼容，改动最小。

### 漏洞 4（高危）：`_evict_one_idle` 只回收终态节点 → 槽位泄漏

```python
# registry.py:170
finished = sorted((n for n in self._nodes.values() if n.is_terminal), ...)
```

`max_active=4` 时，4 个持久化的 idle 队友**永远不算"可回收"**，第 5 次 spawn 必然报
「已达上限」。现在队友跑完就终态，所以能回收；持久化后这条路断了。

**修法**：`_evict_one_idle` 增加第二优先级——先回收 `is_idle` 且 `idle_since` 最久者
（真正在等活的），再回收终态。并与 `close_agent` 联动：被淘汰的 idle 节点要
`_stop_node()`，否则 it 的 `wake` 还挂在事件循环里。

### 漏洞 5（高危）：`has_active()` 让 SSE 被 idle 队友拖满 300 秒

```python
# host.py:626
def has_active(self) -> bool:
    return bool(self.registry.active_nodes())   # idle 队友也算 active
# chat.py:104  main_done 后只要 has_active() 就一直等，直到 TEAM_DRAIN_TIMEOUT_S=300
```

初版设计 §3.6 提到了这点但没给实现。复核后确认这是**必然发生**的：
队友报告完进入 idle，主代理也说完话了，SSE 还要空等 300 秒才断。

**修法**：`has_active()` 拆两个：

```python
def has_active(self):        # 用于 SSE 关闭判定：idle 且无活 → 不算
    return any(not n.is_terminal and not n.is_idle for n in self.registry.all_nodes())

def has_live_nodes(self):    # 用于 close_all / stop_all：idle 也算（要能关掉）
    return bool(self.registry.active_nodes())
```

调用点必须区分：`chat.py::_active()` 用前者，`stop_all_now()` / `close_all()` 用后者。
**这两处混用是本轮最容易引入的 bug。**

### 漏洞 6（中危）：`followup` 与 `deliver_team_message` 的 5 条上限会卡死长期 worker

```python
# host.py:410
async def followup(self, target, message, max_per_agent: int = 5):
    ...
    if len(node.followup_hashes) >= max_per_agent:
        return {"ok": False, "error": "该子代理的追加指令已达上限（5 条）"}
```

一次性队友最多被追加 5 次是合理护栏；持久 worker 长期存活时，
第 6 条消息直接被拒——而它恰恰是"还能继续干活"的那个。

**修法**：上限从「累计条数」改成「**同一轮内**未消费条数」
（即 `len(node.pending_messages) >= 5` 才拒），已消费的计数在新一轮开始时清零。
`followup_hashes` 的**去重**语义保留（防重复投递同一句），但它不该承担限流职责。

### 漏洞 7（中危）：idle 期间 `quota_lease` 与 `notify` 的归属

现在 `finally` 里一次性做四件事：释放配额、写 sidechain、写 run、发通知。
持久化后 `finally` 只在真正终态执行，于是：

- **配额**：idle 期间继续持有 `subagent_lease` 是**对的**（它确实还占着一个槽），
  但 §10.1 的 `idle_timeout` 到了必须确保释放——用 `try/finally` 包住整个循环即可，
  不要在每轮里释放（否则第 5 个 spawn 能挤进来，实际并发超限）；
- **通知**：现在 `_notify` 每轮结束都发，会让 lead 收到一堆"某某已 completed"。
  持久化后应该只在**真正终态**发 `subagent_end`，每轮结束发的是
  `idle_notification`（§3.4），两者语义必须分开。

---

## 4.6 复核后对初版方案的修订

| 初版说法 | 复核后 |
|---|---|
| "已经铺好 80% 的路，缺一个 while" | **改为：循环骨架是小事，7 处时序假设才是主工作量** |
| "`_run_child` 改成 round 循环即可" | 还要同时改 `wait` / `read_result` / `_evict_one_idle` / `has_active` / `followup` / `store` |
| "`has_active()` 加子条件" | 要拆成两个方法，且 `chat.py` 全员复核调用点 |
| "idle 通知帧（P-C）" | `subagent_end` 与 `idle_notification` 必须拆开（漏洞 7），否则 lead 被刷屏 |
| §5 P-A 的验证 | **必须包含上面 7 条的反向确认**，不只是"能跑起来" |

## 5. 实施计划（分阶段，每阶段可独立验证）

### P-A：持久循环骨架（无自动认领、无钩子）

按复核结论排序，**先修语义再用循环**（否则循环一上线等待语义就坏）：

1. **拆 `is_terminal` / `is_idle`**（漏洞 1、2）
   - `is_terminal` 仅表示生命周期结束；新增 `is_idle` 表示本轮完成、在等后续；
   - `wait._condition_met` 改判 `is_terminal or is_idle`，`_wait_result` 加 `ready` 态；
   - `read_result` 允许读 idle 节点，返回 `partial: true`。
2. **拆 `has_active()`**（漏洞 5）
   - `has_active()`（SSE 用，idle 且无活不算）/ `has_live_nodes()`（stop/close 用）；
   - 逐一复核 `chat.py::_active()`、`stop_all_now()`、`close_all()`、`_settle_turn()` 的调用点。
3. **store 层进度更新**（漏洞 3）
   - 新增 `update_run_progress(run_id, turns, result_text)`；`finish_run` 只在终态调；
   - `subagent_runs` 加 `rounds` 列（幂等迁移，与现有 `auto_compact_tokens` 同法）。
4. **槽位回收**（漏洞 4）
   - `_evict_one_idle` 两级优先：先回收 idle 最久者（并 `_stop_node`），再回收终态。
5. **`followup` 限流改为按未消费条数**（漏洞 6）
   - `len(node.pending_messages) >= 5` 才拒；去重哈希保留，但不再承担限流。
6. **通知语义拆分**（漏洞 7）
   - `idle_notification`（每轮）/ `subagent_end`（终态）分开，配额在循环外层 `finally` 释放。
7. **循环本体**
   - `_run_child` 改 round 循环；`run_stream` 结束落"一轮完成"；
   - `AgentNode` 加 `wake` / `idle_since` / `rounds` / `idle_reason`；
   - `_idle_wait`：`asyncio.Event` 为主 + 2s 兜底轮询；
   - idle 超时（`TEAMMATE_IDLE_TIMEOUT_S`，默认 30s）后才进终态。

**验证（缺一不可）**：

| 层 | 内容 |
|---|---|
| 单测 | 状态机（idle→wake→新一轮→超时退出）；`all_terminal` 在 idle 时返回 `ready` 而非挂起；`read_result` 读 idle 返回 `partial`；4 个 idle 队友时第 5 次 spawn 能淘汰最久者；followup 第 6 条在同轮被拒、跨轮允许；`has_active`/`has_live_nodes` 语义分离 |
| 反向确认 | 把 `_condition_met` 改回纯 `is_terminal` → 对应测试必须 RED |
| 真实 e2e | ① 队友报告后不立即结束；② lead 再说一句，队友带上下文接着干（不重新 spawn）；③ `wait_agent(all_terminal)` 不白等 60s；④ 主代理说完话后 SSE **不等满 300s** 就正常结束 |

### P-B：自动认领

- `team_store.find_claimable()`；`_idle_wait` 查到就认领并注入 CC 的 prompt 模板；
- env 开关 `TEAMMATE_AUTO_CLAIM`，**默认关闭**（行为变化太大，先让别人 opt-in）；
- 提示词补充「空闲时会自动认领 pending 任务」。

**验证**：单测（claimable 筛选 / 并发争抢只有一个成功）；
e2e：lead 建 4 个任务不派活 → 1 个队友自己一个个认领做完。

### P-C：idle 通知帧 + 结果预算

- `idle_notification` 结构化帧；
- 显式 DM 去重（照 GA `_sent_to_lead_this_round`）；
- 单条 4000 / 总 16000 字符预算。

**验证**：单测三条（帧形状 / 去重 / 截断）；
e2e：队友自己发过消息时不重复投递 idle 帧。

### P-D（可选，独立）：钩子框架

按 §4.4 的 6 个事件 + `command` handler 起步。**不在 team 的验收范围内**。

---

## 6. 风险与权衡

| 风险 | 说明 | 缓解 |
|---|---|---|
| **SSE 被 idle 队友拖住** | `has_active()` 长期为 true，用户等 300s | §3.6 的 `has_pending_work()`；必须有测试 |
| **token 成本上升** | 队友 idle 时仍在等，每次唤醒都是一次模型调用 | idle 不调用模型（只在有活时调）；idle 预算默认 30s |
| **任务被抢** | 开了自动认领后，lead 想自己做的任务可能被队友抢走 | 默认关闭；提示词说明；`task_create` 时可指定 owner 预留 |
| **行为变化破坏现有编排** | 现在"队友跑完就结束"是模型熟悉的语义 | 提示词明确写；持久化只在本轮 SSE 内生效，跨轮仍是新 spawn |
| **多用户下的资源放大** | 每轮最多 4 个 teammate × idle 30s | 现有 `SUBAGENT_MAX_ACTIVE=4` 与配额闸门已覆盖；idle 不计 token |

### 6.1 一个必须承认的语义折中

跨**用户消息轮次**仍然不能复用同一个 agent。这是 §2.1 决定的：SSE 一关，事件的可见性
就断了，硬要复用只能做到"后台悄悄活着但用户看不见"，比不做更糟。

所以持久化的收益是：**同一轮内**，队友多次被唤醒、持续带着上下文工作。
这与 CC（terminal 常驻）有本质差别，但符合 Web 形态。
**如果用户要的是"跨轮永续 worker"，那需要的是后台任务系统（另一条产品线），不是 team。**

---

## 7. 是否做钩子：给出明确建议

**建议：team 持久化先做（P-A/B/C），钩子单独立项（P-D），两者不耦合。**

理由：

1. **持久化不需要钩子**（§4.1），做了 P-A/B/C 就能拿到 90% 的收益；
2. **钩子的成本在配置面与权限语义**，那是平台级设计，塞进 team 阶段会拖垮整个迭代；
3. **Web 多用户的钩子权限模型没想清楚之前不该做**——"谁有权给别人的会话配 PreToolUse 钩子"
   这个问题现在没有答案，做出来就是安全隐患；
4. 但 §4.2 那个「用户还在活动就多等一会儿」的诉求，**用内置逻辑就能满足**，
   不需要为此引入钩子。

如果一定要现在就回答"要不要做钩子"：**要，但不是现在，也不是为了 team。**

---

## 8. 待决问题（需要你拍板）

1. **持久化窗口到哪为止？** 我建议「本轮 SSE 内」（§2.1/§6.1）。若你要跨轮永续，
   那是另一套设计（后台任务 + 跨轮事件回放），工作量差一个量级。
2. **自动认领默认开还是关？** 我建议关（opt-in），因为它改变"lead 派活"的现有习惯。
3. **idle 超时默认多少？** CC 是 23–30s，GA 可配。我建议 30s，且 `TEAM_DRAIN_TIMEOUT_S`（300s）
   仍是 SSE 的硬上限。
4. **优先做哪个？** 我建议 P-A → P-B → P-C 顺序，P-A 就能让"再算一个"不必重新 spawn。

## 9. 变更记录

- 2026-11-11：首版。基于 CC319 §10/§16.4 与 GA `hooks/` 源码 + 上一轮 D1–D4 加固的实际状态分析。
- 2026-11-11（第二轮复核）：逐条对照代码，发现 7 处逻辑/实现漏洞（§4.5），
  推翻"已铺好 80%"的初判；新增 §4.5 / §4.6，重写 P-A 清单为"先修语义再用循环"，
  验收表加入 6 条单测 + 1 条反向确认 + 4 条真实 e2e。
- 2026-11-11（P-A 实施完成）：按 §4.5 的顺序落地，7 条漏洞全部修复。单测 505 → 524
  （新增 19 条：15 条 `test_team_persistent_worker.py` + 4 条 `test_team_persistent_loop.py`），
  6 组反向确认全 RED。真实湛卢 e2e 5/5 PASS（见 §10）。**默认 `TEAMMATE_IDLE_TIMEOUT_S=0`
  即关闭持久化**，行为与历史完全一致；设为正数才启用。


---

## 10. P-A 实施记录（2026-11-11）

### 10.1 落地结果

| §4.5 的漏洞 | 修法实施 | 验证 |
|---|---|---|
| 1 `wait` 被 idle 卡死 | `_condition_met` 改判 `is_settled`；`_wait_result` 新增 `ready` 态 + `idle_targets` | 单测 + 反向确认 RED |
| 2 `read_result` 拒绝 idle | 允许读，返回 `partial: true` + 说明 | 单测 + 反向确认 RED |
| 3 run 只写一次 | 新增 `update_run_progress()` + `rounds` 列（幂等迁移） | 单测（进度不落终态） |
| 4 槽位泄漏 | `_evict_one_idle` 改为两级：先淘汰最久 idle（并 stop+wake），再终态 | 单测 + 反向确认 RED |
| 5 SSE 被拖 300s | 拆 `has_active()` / `has_live_nodes()` | 单测 + 反向确认 RED |
| 6 followup 5 条封死 | 限流改按 `len(pending_messages)`；去重哈希保留 | 单测 + 反向确认 RED |
| 7 通知语义混 | `idle_notification` 与 `subagent_end` 分开；已 DM 则抑制 idle 帧 | 单测 ×2（正反对照） |

### 10.2 循环本体

`_run_child` 从「跑一次」改成「round 循环」：

```
while True:
    stop?/deadline? → 退出
    _refill_inbox()            # 补投库里积压的团队消息
    _drive() 一轮              # run_stream 跑到 done
    rounds += 1; _snapshot_run()   # 写进度，不写终态
    if idle_timeout <= 0 → completed（历史行为）
    status = idle; 推 idle 帧（已 DM 则抑制）
    reason = _idle_wait(node)  # Event 为主 + 2s 兜底轮询
    stopped / timeout → 退出；wake → 回到 running
```

### 10.3 一个实现中发现的额外问题（复核时没预见到）

**`stop_all_now()` 会抢在 `_stop_node(drain)` 前面把状态改成 `stopped`。**
本轮 SSE 结束时 `_settle_turn` 先调 `stop_all_now()`，它无条件 `task.cancel()`，
于是子任务走 `CancelledError` 分支写 `stopped`，覆盖了"它其实干完过 2 轮"的事实。

修法：三处都加"尊重已判定终态"的判断——`CancelledError` 分支、
循环顶部的 `stop.is_set()` 分支、`idle_wait` 返回 `stopped` 的分支，都是
`if not node.is_terminal: node.status = ...`。`stop_all_now()` 里对
`rounds > 0 and not error` 的节点先判 `completed`。

真实 e2e 复验：`runs=[{status: 'completed', rounds: 2, turns: 5}]` —— 修复前是 `stopped`。

### 10.4 默认值决策（重要）

`TEAMMATE_IDLE_TIMEOUT_S` **默认 0（关闭）**。理由：

1. 持久化改变的是产品语义（队友跑完还在不在），不是纯内部优化；
2. 全量回归里有 7 个既有测试依赖"跑完即终态"，说明这个假设在代码里是广泛的；
3. 关闭时行为与历史 100% 一致，等于一个零风险的能力开关。

启用方式：`TEAMMATE_IDLE_TIMEOUT_S=45 python run.py`。

### 10.5 真实 e2e 现场

`work/_persist_e2e_20261111_TR2.py`，runId `persist-093445-edf2`，5/5 PASS：

- `starts=['keeper']` —— 全程只 spawn 一次；
- `statuses=[('keeper','idle'), ('keeper','running'), ('keeper','idle')]` —— 真的经历了 idle→唤醒→再 idle；
- 第二轮回答 `4242`（第一轮写进上下文的数字），证明**上下文被保住**；
- `subagent_runs: {status: completed, rounds: 2, turns: 5}`；
- 模型自述「没有重新 spawn，两轮共用同一 run_id」。

### 10.6 仍未做

- **P-B 自动认领**：`find_claimable()` + CC 的 prompt 模板 + `TEAMMATE_AUTO_CLAIM` 开关（默认关）。
- **P-C idle 通知帧的结果预算**：单条 4000 / 总 16000 字符截断（帧本身已实现）。
- **P-D 钩子**：按 §7 的结论单独立项。
- **跨轮持久**：按 §2.1 / §6.1 的判断不做——那需要后台任务系统，是另一条产品线。
