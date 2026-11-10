# wait_agent 异步化 + 用户中途输入（queued prompt）设计

> 上半部分是本轮已实施并验证的内容；下半部分是对标 CC 2.1.319 与 GenericAgent
> 后给出的 Hlaudz 实现方案（尚未实施）。
> CC 侧证据来自本仓库 `docs/20261024-CC-执行中继续输入机理调研.md`（含 `m0459` 等模块坐标），
> GA 侧证据直接来自 `D:/git_codes/GenericAgent`（只读）。

---

## 1. 已实施：`wait_agent` 不再阻塞回合

### 1.1 改了什么

| | 改前 | 改后 |
| --- | --- | --- |
| 默认条件 | `all_terminal`：一直等到所有子代理结束 | `any_event`：等到**第一个实质进展**就返回 |
| 返回 | 只有 `timed_out` + 全量状态 | 三态 `state`: `event` / `all_terminal` / `timeout` |
| 事件游标 | 每个节点各自从 0 计数的 `event_seq` | 宿主级全局 `_gseq`（多子代理时不再串味） |

实质进展的定义（`host.py:_WAKE_EVENTS`）：`tool_start` / `tool_result` / `message_injected`。
**文本增量不算**——否则每个 token 都会唤醒等待者，把回执刷成半句话。

### 1.2 为什么这样改而不是直接删掉 wait

直接删掉 `wait_agent` 会让模型失去「同步等一次」的能力，S10 那类「必须拿到结果才能继续」的
场景会退化成轮询。改成默认非阻塞、显式可要求全等，两种语义都保留：

- `state=event`：有进展没结束 → 母代理可以去做别的，稍后再 wait；
- `state=timeout`：这段时间没进展，子代理仍在跑，**不是失败**；
- `state=all_terminal`：全部结束，此时 `read_agent_result` 取权威结果。

`condition=all_terminal` 仍然可以显式传，行为与改前一致。

### 1.3 验证

| 层 | 内容 | 结果 |
| --- | --- | --- |
| 单测 | `test_wait_any_event_returns_before_child_finishes`（<2s 返回、remaining 非空） | PASS |
| 单测 | `test_wait_any_event_timeout_state`（无事件 → state=timeout） | PASS |
| 单测 | `test_wait_any_event_returns_all_terminal_when_done` | PASS |
| 单测 | `test_wait_tool_defaults_to_any_event`（工具层默认值） | PASS |
| 单测 | `test_teaches_nonblocking_wait_semantics`（提示词讲清三态） | PASS |
| 反向 | stash `host.py` + `tools.py` → 4 条测试全红 | 已确认 |
| 回归 | `pytest -q` | 419 passed |

---

## 2. 现状：Hlaudz 为什么现在「中途输入」做不到

两处刚性的拦截，缺一不可拆：

1. **前端直接吞掉**。`frontend/src/App.tsx` 里 `send()` 开头就是
   `if ((!text.trim() && attachments.length === 0) || streaming) return`
   ——流式期间按 Enter 什么都不会发生，输入框里的文字还留在原地，用户以为程序卡了。
2. **后端只允许一个 generation**。`app/routers/chat.py:681` 用
   `team_store.acquire_generation()` 拿租约，拿不到直接 `409 generation_active`，
   hint 写的是「请先点击停止」。

所以这不是「加个队列」的小改动，而是要把「同一会话同时只有一条生成」这个不变量，
改成「一条生成 + 一个可随时写入的输入通道」。

---

## 3. CC 2.1.319 的做法（结论摘要，坐标见调研文档）

CC 没有「把新输入塞进正在进行的模型请求」这种能力——一次 HTTP 请求发出后无法追加。
它做的是**命令队列**，在三个时机消费：

| 时机 | 行为 |
| --- | --- |
| 轮内 fold | 每个工具批次结束后，把 `priority=next` 的用户消息作为 `queued_command` attachment 插进同一回合 |
| 轮末 | 当前回合结束，drain 队头开下一回合 |
| `priority=now` | 立刻 abort 当前回合，让这条消息马上开始 |

几个值得抄的设计：

- **单一全局队列**（`m0459`），主线程 / 子 agent / 任务通知 / peer 消息共用，靠 `mode`/`priority`/`agentId` 区分；
- **三档优先级** `now=0 / next=1 / later=2`：人类输入默认 `next`，任务通知 `later`；
- **模型看到的是被包装过的 user 文本**，不是队列结构（`The user sent a new message while you were working:`）；
- **入队前过 `prompt.submit` hook**，被拒的消息退回草稿而不是静默消失；
- **Esc 清队列**、**上箭头把排队消息拉回输入框**、结果里带 `queued_turn_count`。

---

## 4. GenericAgent 的复刻（比 CC 简单得多，值得直接抄）

GA 没有做优先级、hook 筛选、队列编辑，只做最小闭环，代码集中在三处：

| 位置 | 作用 |
| --- | --- |
| `genericagent/core/pending_input.py` | `PendingPromptQueue`：线程安全、`drain()` 原子、**故意跨回合存活** |
| `genericagent/core/loop.py:_consume_queued_prompts` | 在「工具批次之后、下一次 `chat()` 之前」把队列渲染成块注入 |
| `frontends/ink/bridge.py:480-515` | 回合在跑就 `queue.push(text)` 并 emit `prompt_queued`；回合已结束则提升为普通 submit |

它自己的注释把三个坑写得很清楚，Hlaudz 会原样踩到：

1. **队不能挂在回合上**：「a message that arrives after the last fold point of turn N
   must survive into turn N+1, which is exactly the case a per-turn buffer gets wrong」。
2. **drain 必须原子**：UI 线程 push、回合线程 drain，读-清两步会丢掉中间到达的消息。
3. **注入文本必须能区分**：用 CC 的包装文案，否则模型会重新回答原来那个问题。

而且它的注释直接点出了和 `wait_agent` 的关系：

> it is also the point a long synchronous tool call (`wait_agent`) eventually reaches,
> **so the wait stops being a black hole**.

这正是上面 §1 要解决的另一半：wait 不再是黑洞（会带着 `state=event` 回来），
用户消息也就能在同一个 fold 点被吸收。

GA 还处理了一个竞态：UI 决定排队、请求到达时回合刚好结束 →
**提升为普通 submit**，而不是塞进一个再也等不到 fold 点的队列。

---

## 5. Hlaudz 怎么实现（方案，尚未实施）

### 5.1 先看清约束：Hlaudz 和 CC/GA 的形态不同

| | CC / GA | Hlaudz |
| --- | --- | --- |
| 进程形态 | CLI/Ink，单进程内存队列 | FastAPI + 每请求一个 SSE 流 |
| 「忙碌」的判定 | 进程内 `is_running` | DB 租约 `acquire_generation` + 进程内 task 注册表 |
| 第二条的现状 | 入队 | **409 generation_active** |
| 前端 | 自己就是进程 | 独立 SPA，流式期间 `if (streaming) return` 直接吞掉 |
| 注入点 | 已有 fold 点 | **已有**：`AgentLoop.message_injector`（现在只喂子代理通知） |

结论：注入点不用新建，**要新建的是「活着能写进去的队列」和「一个不返回 409 的入口」**。

### 5.2 最小闭环（P0，建议先做这一版）

1. **队列对象**：`app/steer_queue.py`

   ```python
   class SteerQueue:
       def push(self, text, *, source="user", priority="next") -> bool: ...
       def drain(self, limit=5) -> list[dict]: ...   # 原子：取走即清
       def snapshot(self) -> list[dict]: ...          # 给 UI 回显，不消费
       def clear(self) -> int: ...
   ```

   存放位置：挂在**进程内 generation 注册表**上，键 `(user_id, session_id, generation_id)`，
   而不是挂在 `TurnRuntime` 上——回合结束要保留，直到下一个 generation 起跑后 drain 一次。
   （GA 的第 1 条坑：队列必须跨回合。）

2. **后端入口**：`POST /api/sessions/{sid}/messages` 增加 `mode: "queue"`。
   - 若 `is_generation_live(...)` 为真 → 不抢租约，`queue.push(text)`，返回 `202 {"queued": true, "queue_depth": n}`；
   - 若当前没有活着的 generation（竞态：刚好结束）→ **降级为普通发送**，返回正常的流式响应
     （GA 的同一处理）；
   - 否则维持现在的行为。

3. **fold 注入**：`subagent/session.py` 里已经有一行

   ```python
   message_injector=host.drain_notifications,
   ```

   改成组合注入器，**用户消息优先于子代理通知**（用户的是新指令，通知是旧事实）：

   ```python
   def _inject():
       msgs = steer_queue.drain()
       msgs += host.drain_notifications()
       return msgs
   ```

   注意 `message_injector` 每轮只在模型调用前调用一次，返回的 list 直接 append 进历史，
   所以注入内容要自带包装文案。

4. **包装文案**（照抄 CC，改中文收口）：

   ```text
   The user sent a new message while you were working:
   <原文>

   这是用户在上一轮执行过程中补充的要求，请在本回合内一并处理，不要当成新的对话回合。
   ```

5. **事件回显**：新增 SSE 事件 `steer_queued {text, queue_depth}` 与 `steer_consumed {ids}`。
   `agent_loop` 已经有 `message_injected` 事件（上一轮为子代理通知加的），复用即可；
   前端从「输入被吞」变成「消息立刻上屏 + 標注已排队」。

6. **前端**：`App.tsx` 的 `send()` 在 `streaming` 时不再 `return`，改为
   `POST ... {mode:"queue"}`，本地把这条消息以 `pending` 状态显示（气泡右侧加「已排队」角标），
   收到 `steer_consumed` 后转成正常消息。

### 5.3 P1：优先级与中断（对标 CC 三档）

| 档 | 触发 | 行为 |
| --- | --- | --- |
| `now` | 前端「立即发送」按钮 / 快捷键 | 请求里带 `priority:"now"` → 后端置 `stop` + 队列置顶，本回合中止，消息作为新回合开头 |
| `next` | 普通回车 | 下一次 fold 注入（P0 的默认） |
| `later` | 子代理通知 / 系统事件 | 现在就是这条路径（`drain_notifications`） |

实现要点：`now` 不能只「插到队头」——当前回合还在跑，模型可能几十秒后才到 fold 点。
必须走已有的停止链路（`request_stop` / `stop_event`），并把这条消息标记为「中止原因 = 用户插话」，
否则用户会看到 `stopped` 加一条孤立的排队消息。

### 5.4 P2：多标签与持久化

- **持久化**：Hlaudz 是多标签页 Web，队列只在进程内存里 = 刷新就丢。
  建议落 `session_steer_queue(id, session_id, generation_id, text, priority, created_at, consumed_at)`,
  与 `subagent_notifications` 同一套 peek/ack 模式（那张表已经验证过跨回合可用）。
- **队列可编辑**：上箭头把排队消息拉回输入框（CC 的 `popEditableAt`）；
  需要一个 `DELETE /messages/queue/{id}`。
- **Esc 清队列**：停止时默认清空人类排队消息，系统通知保留（CC 的 `cleared_on_cancel` / `keep` 谓词）。
- **队列深度**：SSE `done` 事件里带 `queued_turn_count`（CC 的 `m0120:7756`），
  前端可显示「还有 2 条待处理」。

### 5.5 明确不做

- **`prompt.submit` hook 式的前置筛选**：Hlaudz 的 hooks 面还没做到这一步，收益也低；
- **peer/子代理消息与用户消息共用一个队列**：CC 是单队列靠 `mode` 区分，Hlaudz 的两条来源
  生命周期不同（通知要落库、要跨回合），合并反而更容易错，保持两个队列 + 组合注入更清楚。

---

## 6. 验收（实施后必须跑）

| # | 场景 | 判据 |
| --- | --- | --- |
| 1 | 单元 | `SteerQueue` 的 push/drain 原子性：并发 push 时 drain 不丢消息 |
| 2 | 单元 | 注入器合并顺序：用户消息在子代理通知之前 |
| 3 | 单元 | 竞态：generation 已结束时 `mode:"queue"` 降级为普通发送 |
| 4 | e2e S15 | 长任务（sleep 90）执行中投递第二个问题，**同一次生成**内被读到并回应，不产生第二个 generation |
| 5 | e2e | 上述过程中前端始终能输入；`queued_turn_count` 与实际队列一致 |
| 6 | 回归 | 现有 S1–S11 + `pytest -q` 全绿 |

> 第 4 条的关键判据是「**没有出现第二个 generation**」——否则只是把 409 换成了串行排队，
> 用户的诉求（边跑边问、当场被回答）并没有满足。

---

## 7. S14 实测：非阻塞之后模型的行为变了

场景：派一个 sleep 90 秒的子代理，明确要求「派完不要干等，先做点别的，再回来看结果」。

真实工具序列（湛卢，275.7s）：

```
spawn_agent → file_glob → list_files → file_glob → read_agent_result → file_glob → close_agent
```

**它一次都没调 `wait_agent`**：派完立刻去做本地统计，子代理的完成通知在中途到达，
于是直接 `read_agent_result` 取结果。模型的自我说明：

> 结果在我准备调 `wait_agent` 之前，子代理的完成通知就已经到了，
> 所以我没走 wait 循环，而是直接用 `read_agent_result` 取了权威结果。

这条实测比单测更有价值：它说明「通知 fold 注入 + wait 非阻塞」合起来之后，
母代理的自然行为从「派完就阻塞等」变成「派完继续干活，通知来了就收」。
这也顺带说明了为什么 §2 那条队列改造值得做——**母代理已经会「边跑边看消息」，
用户的消息现在还没有路进去**。

> 一个反面教训：这条场景第一次被我判成 FAIL，因为判据写的是「必须出现 `wait_agent`」。
> 判据与改造目标自相矛盾——非阻塞的目标恰恰是**不需要** wait。已改为
> 「spawn 恰好 1 次 + 期间做了本地工作（`file_glob`）+ 用 `read_agent_result` 拿到结果」。
