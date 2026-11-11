# Hlaudz Agent Team 是怎么跑的

> 配套图：[`hlaudz-agent-team-diagram.html`](./hlaudz-agent-team-diagram.html) —— 可交互，支持明暗主题、搜索、聚焦、「动态」开关（信号持续流动）
>
> 基础机制见 [`hlaudz-subagent-mechanism.md`](./hlaudz-subagent-mechanism.md)：Team 里的 teammate 就是 subagent 加了一层持久任务板。
>
> 代码版本：`438327f`（图中每条引用都标注了文件与行号，可用 `git show 438327f:<path>` 复核）

---

## 一句话版本

lead（就是主对话的 root agent）用 `task_create` 把活写进 **SQLite 的 `team_tasks` 表**，
再用 `spawn_teammate(team_task_id=...)` 起 teammate。**绑定动作本身就等于认领**——
服务端把任务置 `in_progress` 并写 owner，teammate 不用再 claim。
teammate 干完用 `task_update(complete)` 交回，用 `send_message` 汇报；
平台在收件人下一次模型调用前把消息注入成 `[来自 X 的团队消息]`。

---

## 和 subagent 的唯一区别

| | subagent | Agent Team |
|---|---|---|
| 本质 | **一次执行**，跑完即结束 | **持久协作事实** |
| 存储 | 侧链表 `subagent_runs` | `team_tasks` / `team_task_deps` / `team_messages` |
| 刷新页面 | 任务没有 | 任务板还在 |
| 依赖排序 | 做不到 | `blocked_by` 硬约束 |
| 用户可见进度 | 只有一张卡片 | 右侧 TeamPanel 任务板 |

teammate 在进程层面就是 subagent（同样是进程内 asyncio 任务、同样继承全部业务工具），
多出来的是「它绑着一条任务、要向 lead 汇报」。

---

## 什么时候会说这些词

**模型是靠自己判断的**，它在这两种方案之间选：

| 要走 Team，说这些 | 会走 subagent |
|---|---|
| 「建个任务板」 | 「派两个子代理」 |
| 「拆成 N 个任务」 | 「并行跑一下」 |
| 「我要看到进度」 | 「分头查一查」 |
| 「任务 A 完成后再做 B」 | |

**要稳定触发，直接点名工具**：「用 task_create 建任务，再用 spawn_teammate 派给 worker」。
界面上 TeamPanel 标题旁的「怎么触发？」也写了这几条。

---

## 八个组成部分

| 组件 | 是什么 | 代码位置 |
|---|---|---|
| Lead agent | 主对话的 root agent，唯一能调编排工具的一方 | `backend/app/team_host.py:18` |
| Team 工具层 | lead 有 8 个，teammate 只有 5 个（少了 `task_create` / `spawn_teammate` / `team_status`） | `backend/app/team_tools.py:212` |
| 任务板 `team_tasks` | `status` / `owner_agent_id` / `version`（CAS） | `backend/app/team_store.py:79` |
| 依赖 `team_task_deps` | `blocked_by`，任务间顺序的硬约束 | `backend/app/team_store.py:99` |
| 消息 `team_messages` | `queued → delivered → consumed` 三态 | `backend/app/team_store.py:111` |
| teammate | 进程内 asyncio 任务，继承全部业务工具 | `backend/app/team_host.py:262` |
| TeamPanel | 右侧面板，SSE 增量更新 + 刷新时 REST 恢复 | `frontend/src/components/TeamPanel.tsx:114` |
| 事件日志 | `team_events`，可用 `tools/team_timeline.py` 还原协作时间线 | `backend/app/team_store.py:348` |

---

## 一次完整链路

1. **建任务**：lead 调 `task_create`，任务以 `pending` 落进 `team_tasks`。
2. **派单**：`spawn_teammate(team_task_id="...")`。**这一步同时完成认领**：
   任务变 `in_progress`，`owner_agent_id` 写成 teammate。
   → 所以 teammate **不要**再 claim 同一任务，两条路径会打架。
3. **干活**：teammate 正常调工具、读写 workspace。
4. **交付**：用 `task_get` 读到的 `version` 调 `task_update(complete)`。
   **必须带 `expected_version`**（CAS）——并发下这是唯一防止互相覆盖的机制。
5. **汇报**：`send_message(recipient="lead", ...)` 落进 `team_messages`。
   teammate 只能发给 lead/root，**不能互相广播**。
6. **注入**：收件人下一次模型调用前，平台把消息变成
   `[来自 X 的团队消息]` 出现在它的上下文里。发送方不需要催对方去读。
7. **收尾**：lead 汇总后 `close_agent` 释放槽位。回合结束时 `close_all()` 落库。

---

## 三件容易误解的事

### 1. 「绑定了还要认领吗」——不用

`spawn_teammate(team_task_id=...)` 的服务端绑定动作已经把任务置 `in_progress` 并写 owner。
只有**未绑定**的任务才需要 `task_update(action="claim")`。

### 2. 「blocked_by 能绕过吗」——不能

被阻塞的任务既不能 `claim`，也不能被 `spawn_teammate` 绑定——**两条路径都返回 `blocked`**。
想跑它必须先让依赖任务 `completed`。

### 3. 「刷新后还能继续吗」——只能恢复任务板

任务、依赖、消息都在 SQLite，刷新后 TeamPanel 能恢复。
但**已结束的 live teammate 恢复不了**——那是一段运行期上下文，不是数据。

所以长任务要把结论写进任务描述或产物文件，不能只留在对话里。

---

## 为什么用 SQLite 而不是 JSON 文件

| | 文件 + 锁 | SQLite 事务 |
|---|---|---|
| 并发写 | 靠文件锁，容易漏 | 事务保证 |
| CAS 版本 | 自己维护 | `version` 列 + `UPDATE ... WHERE version=?` |
| 查询 | 全量读入内存过滤 | 索引 + WHERE |
| 崩溃恢复 | 可能写坏 | WAL + 事务回滚 |

代价是每次操作都要 IO，但 Team 的操作频率是「几十次/轮」，远低于需要优化的量级。

---

## 常见问题

**Q：teammate 能再派子代理吗？**
不能。`spawn_teammate` 这个工具**只存在于 lead 的 registry**，teammate 的 registry 里根本没有它。
这是机制约束，不是靠提示词写「不要派生」。

**Q：teammate 之间能直接对话吗？**
不能。teammate 只能 `send_message` 给 lead/root，不能互相广播。
这是为了避免 N² 的消息风暴。

**Q：一个会话能同时有几个 Team？**
每轮生成创建一个隔离 Team，`USER_MAX_ACTIVE_TEAMS=1`。

**Q：Team 创建失败会怎样？**
只发一条 `team_unavailable` 事件，**不影响普通对话**。Team 是可选协作面。

---

## 相关文档

- [`hlaudz-subagent-mechanism.md`](./hlaudz-subagent-mechanism.md) —— subagent 机制（Team 的基础）
- `docs/20261106-AgentTeam-Web多用户隔离设计方案.md` —— 四级作用域、任务所有权、资源闸门
- `docs/20261106-changelog-AgentTeamP3.md` —— P3 已完成清单与真实 E2E 验收
- `docs/20261111-AgentTeam韧性设计与实施.md` —— API 中断自愈、任务重派