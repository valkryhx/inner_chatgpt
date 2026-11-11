# Hlaudz Subagent 是怎么跑的

> 配套图：[`hlaudz-subagent-diagram.html`](./hlaudz-subagent-diagram.html) —— 可交互，支持明暗主题、搜索、聚焦、「动态」开关（信号持续流动）
>
> 这是 Agent Team 的基础：先看懂 subagent，Team 只是「给它加了一块持久任务板」。
>
> 代码版本：`438327f`（图中每条引用都标注了文件与行号，可用 `git show 438327f:<path>` 复核）

---

## 一句话版本

root agent 调 `spawn_agent`，`SubagentHost` 建一个 agent 节点、先占并发名额，
再用 `asyncio.create_task` 在**同一个进程里**跑一个独立的 agent 循环。
子代理有完整的业务工具（文件读写、`code_run`、Skill、MCP），但**没有编排工具**——
所以它无法递归派生子代理。过程和结果写进**侧链表**，不污染主对话历史。

---

## 为什么不用独立进程

| | 独立进程（GenericAgent 的做法） | 进程内 asyncio（Hlaudz） |
|---|---|---|
| 启动开销 | 每次几百毫秒 | 几乎为零 |
| 停止 | 要发信号 + 等待 + 兜底杀 | `task.cancel()` |
| 共享状态 | 要 IPC | 直接读同一个 SQLite |
| 隔离边界 | 进程级（更强） | 逻辑级（靠 registry 裁剪） |

Hlaudz 是 Web 多用户服务，进程内方案省掉了 IPC 和进程管理，
代价是「隔离靠代码约束而不是操作系统」——所以编排工具的缺席必须是**机制**保证，不能靠提示词。

---

## 八个组成部分

| 组件 | 是什么 | 代码位置 |
|---|---|---|
| Root agent | 主对话循环，唯一持有编排工具的一方 | `backend/app/subagent/session.py:190` |
| 编排工具 | `spawn_agent` / `wait_agent` / `read_agent_result` / `list_agents` / `close_agent` | `backend/app/subagent/tools.py:19` |
| SubagentHost | 生命周期宿主：派生、等待、停止、回收 | `backend/app/subagent/host.py:85` |
| 容量闸门 | 每用户 8 个、全局 64 个活跃子代理 | `backend/app/quota.py:30` |
| SubagentRegistry | 名字、深度、槽位、重名与淘汰规则 | `backend/app/subagent/registry.py:148` |
| 子代理 runtime | 独立 agent 循环，继承全部业务工具 | `backend/app/subagent/runtime.py` |
| 侧链表 `subagent_runs` | 过程与结果，**不写 `session_history`** | `backend/app/subagent/store.py:17` |
| 任务通知 | `[任务通知]` 注入根代理下一轮上下文 | `backend/app/subagent/host.py:606` |

---

## 一次完整链路

1. **派生**：root 调 `spawn_agent(task_name, message, ...)`。
2. **准入**：`registry.create_child` 检查深度（`max_depth=2`）与重名；
   `quota.subagent_lease` 占一个并发名额。**拿不到名额就直接拒绝，不留半成品节点**。
3. **运行**：`asyncio.create_task` 跑子循环。子代理有自己的 history，
   但**看不到父对话**——只能看到 `message` 里写的东西。
4. **过程**：子代理每一步的事件（`text_delta` / `tool_start` / `tool_result`）
   通过主对话同一条 SSE 流出去（`subagent_event`），前端渲染成可折叠卡片。
5. **写侧链**：过程与最终结果写进 `subagent_runs`，**不进 `session_history`**。
6. **通知**：子代理结束时，平台把 `[任务通知] ...` 拼进根代理**下一次模型调用前**的上下文。
7. **取结果**：根代理用 `read_agent_result` 读权威结果（不是 stdout）。
8. **释放**：`close_agent` 释放槽位。

---

## 四件容易误解的事

### 1. `wait_agent` 默认**不等完**

默认 `condition=any_event`：等到**第一个实质进展**就返回，此时子代理还在跑。

| 返回 | 含义 |
|---|---|
| `state=event` | 有进展，它还在跑，你可以先做别的 |
| `state=ready` / `all_terminal` | 全部结束，可以取结果 |
| `state=timeout` | **这段时间没进展，它不是失败**，仍在运行 |

要一次性等完必须显式传 `condition=all_terminal`。
因为 `timeout` 就重复 `spawn` 会白占一个槽位。

### 2. 子代理**看不到**父对话

它只有你写进 `message` 的内容。所以任务书必须自包含：
背景、涉及路径、约束、期望输出、**以及「什么算完成」**。

写「基于你之前的发现」这类空指代是无效的——它没有「之前」。

### 3. 编排工具**不存在**于子代理的 registry

不是「提示词告诉它别派生」，而是 `spawn_agent` 这个工具**根本没被注入**。
这是机制约束，无法绕过。

### 4. 侧链历史不进主对话

子代理的中间产物写 `subagent_runs`，主对话只留一条结论。
否则一次派活就会把主上下文撑满，还会污染后续推理。

---

## 深度与槽位

| 限制 | 值 | 位置 |
|---|---|---|
| 最大派生深度 | 2（root → 子代理） | `registry.py:148` |
| 每用户活跃子代理 | 8 | `quota.py:30` |
| 全局活跃子代理 | 64 | `quota.py:31` |
| 单次墙钟超时 | 300s（可传 `timeout_seconds` 覆盖） | `host.py` |

深度为 2 意味着**子代理不能再派子代理**——加上编排工具缺席，是双重保证。

---

## 和 Agent Team 的关系

| | subagent | teammate |
|---|---|---|
| 进程层 | 进程内 asyncio | **完全相同** |
| 业务工具 | 全部继承 | **完全相同** |
| 编排工具 | 没有 | 有 `send_message` / `task_*`，但**仍不能 spawn** |
| 多出来什么 | — | 绑着一条 `team_tasks` 记录，要向 lead 汇报 |

所以看懂这张图，Team 只剩「任务板 + 消息 + CAS」三个新概念。

---

## 常见问题

**Q：子代理能读父对话的历史吗？**
不能。只能读 `message` 里写的内容。这是隔离设计，不是缺陷。

**Q：子代理失败会留下僵尸吗？**
不会。失败走 `finally` 分支，`finish_run` 写终态、释放名额、`unregister` 租约。

**Q：能同时派几个？**
受 `USER_MAX_ACTIVE_SUBAGENTS=8` 限制，超了在 `spawn` 阶段就拒绝。

**Q：子代理的产物在哪？**
文件在 `backend/data/workspaces/<user_id>/`，记录在 `subagent_runs` 表。

---

## 相关文档

- [`hlaudz-agent-team-mechanism.md`](./hlaudz-agent-team-mechanism.md) —— Agent Team（在 subagent 之上加任务板）
- `docs/20261105-changelog-SubagentP2.md` —— P2 已完成清单与自测
- `docs/20261105-Subagent调研与设计方案.md` —— 进程树 vs 进程内的调研对比