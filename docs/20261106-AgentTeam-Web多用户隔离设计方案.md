# Agent Team：Web 多用户隔离与稳健性设计方案

> 摘要：在 P2 Subagent 之上实现 Team-lite，但把团队、任务、消息和运行时严格绑定到 `user_id + session_id + generation_id`；完整隔离不仅要靠查询条件，还要靠工具闭世界、任务/消息所有权、用户级配额、全局公平调度和故障域隔离。  
> 日期：2026-11-06  
> 状态：P3 基线已实施（2026-11-06）；实施记录见 `docs/20261106-changelog-AgentTeamP3.md`
> 前置：P2 Subagent 已完成；下一阶段仍不引入 LangChain / LangGraph，也不照搬 GA CLI 总装层。

---

## 1. 结论与范围

### 1.1 结论

下一步做 **Agent Team-lite**，但必须先承认 GA 的 Agent Team 是“单机、单本地用户、进程树 + 文件 IPC”模型；本项目是“多用户 Web、SQLite、SSE、进程内 asyncio”模型。两者不能只替换存储路径，必须重新定义作用域和资源边界。

推荐的第一版定义：

- 一个用户可拥有多个会话；
- 一个会话同一时刻最多一个活跃 Team；
- 一个 Team 只属于一个 root generation；
- root 自动是 lead，子代理是 teammate；
- 任务板是进度事实来源；
- 消息只做 lead ↔ teammate 的受控协作；
- teammate 不能 spawn、不能关闭其他 agent、不能跨团队发消息；
- Team 的 live runtime 默认不跨 generation 存活；
- 任务和消息持久化，用于审计、刷新恢复展示和异常回收；
- 任意 Team 的停止、失败、配额耗尽只影响自己，不能暂停其他用户或其他会话。

一句话：**Team 是现有 SubagentHost 之上的协作层，不是新的全局调度器，也不是 Dynamic Workflow。**

### 1.2 明确不做

第一版不做以下能力：

- 不做跨用户团队；
- 不做跨会话团队；
- 不做全局公共任务板；
- 不做 teammate 间自由广播和无限对话；
- 不做 teammate 递归 spawn；
- 不做 Team 在 HTTP 请求结束后继续后台常驻；
- 不做多实例之间迁移 live teammate；
- 不做自动重试整个团队；
- 不做完整 DAG Workflow。

这些不是保守，而是为了避免把 Team 直接演化成一个缺少持久化调度、租约和恢复机制的半成品 Workflow。

---

## 2. GA Team 的单用户假设审计

### 2.1 GA 的实际形态

GA 的 Team 核心建立在：

- 一个本地会话 / 一个本地工作目录；
- 每个子代理独立 OS 进程；
- 注册表、任务板、邮箱都是文件系统状态；
- lead 名称固定为 `team-lead`；
- 团队名由 session id 派生，且可由 `GA_TEAM_NAME` 覆盖；
- swarm 通过 `base_dir` 做进程内缓存单例；
- 多个进程通过 lock + atomic write 共享文件状态；
- 权限、路径 ACL 主要围绕本地 workspace 和当前调用用户。

GA 的 `SubagentTaskStore` 已比“玩具级多智能体”可靠，包含：

- 单 owner 软锁；
- `blockedBy` 依赖；
- 并发 claim 单赢家；
- owner 回收；
- 消息去重与投递状态；
- 已读重试；
- 终止后回收任务。

这些语义值得借鉴，但不能直接复制其作用域。

### 2.2 进入 Web 多用户后新增的差异

| 维度 | GA CLI | 本项目 Web |
|---|---|---|
| 用户 | 隐含只有本机用户 | 必须显式 `user_id`，且所有读写强制过滤 |
| 团队身份 | session 派生或环境变量覆盖 | 不能信任环境变量，必须服务端生成并绑定 owner |
| 进程模型 | 每个 agent 一个 OS 进程 | 同一 FastAPI 进程内多个 asyncio task |
| 共享状态 | 文件锁 + atomic write | SQLite 事务 + 进程内 registry + 单实例护栏 |
| 跨会话 | 用户通常只有一个会话 | 同一用户可并行多个会话，不能串任务板/邮箱 |
| 模型客户端 | 本地配置 | 每个用户可能不同 LLM / API Key / base_url |
| MCP | 本地共享配置 | 每个用户独立 MCP，可能含私密凭据和本地 stdio 进程 |
| Skill | 本地目录 | builtin/shared/user 三层，私有路径按 user 隔离 |
| workspace | 本地项目目录 | `backend/data/workspaces/<user_id>/`，同一用户多个会话仍需区分产物 |
| 停止 | 本地 Ctrl-C / 关闭进程 | 任一会话的 stop 不能影响其他会话 |
| 失败 | 本机可见 | 必须隔离故障域并给用户可理解的状态 |
| 配额 | 通常不做 | 必须限制单用户并发，防止一个用户耗尽全站 |
| 审计 | 本地日志 | 任务、消息、结果必须可按 user/session 查询且不可越权 |
| 多实例 | 一般不考虑 | 至少设计成未来可替换为 DB/队列，不埋进程内 list 的坑 |

### 2.3 GA 中可直接借鉴、但不能照搬的部分

可直接借鉴：

- 任务状态只用 `pending / in_progress / completed` 起步；
- `owner` 是软锁，claim 必须 CAS；
- `blockedBy` 只显示未完成依赖；
- 完成任务必须由 teammate 显式执行；
- teammate 终止后回收其未完成任务；
- 消息必须有稳定 message id，支持幂等；
- lead 不应重复接收已明确 DM 的同一结果；
- 不要在 turn 结束后自动把所有 in-progress 任务标完成。

不能照搬：

- `GA_TEAM_NAME` 环境变量覆盖团队身份；
- 固定 `team-lead` 作为跨团队唯一 lead；
- `_SWARM_CACHE` 仅按 `base_dir` 做全局单例；
- 以文件路径作为主要权限边界；
- 一个进程里通过文件名隐式共享任务板；
- 由 root 自己随意指定任意 owner 路径；
- 默认允许 peer-to-peer 广播；
- 把“所有文件都在本地”当作天然隔离。

---

## 3. 四级作用域模型（硬约束）

### 3.1 作用域链

所有 Team 对象必须固定下面这条链：

```text
user_id
  └── session_id
        └── generation_id
              └── team_id
                    ├── task_id
                    └── message_id
```

不是“只带 user_id 就够了”，也不能“只带 session_id 就够了”。

- `user_id` 是租户边界；
- `session_id` 是对话与工作上下文边界；
- `generation_id` 是一轮生成与 live runtime 边界；
- `team_id` 是协作状态边界。

### 3.2 服务端生成身份

以下字段一律由服务端生成，不接受模型或前端直接指定：

- `team_id`
- root `agent_id`
- teammate `agent_id`
- `message_id`
- `created_at`
- `sender` 的真实身份

模型只能提供：

- `subject`
- `description`
- `blockedBy`（且只允许本团队任务 id）
- `recipient` 的 teammate 名称
- 消息正文

### 3.3 任何库操作必须四条件

任务和消息的读取、更新、认领、删除，必须同时校验：

```sql
WHERE task_id = ?
  AND user_id = ?
  AND session_id = ?
  AND generation_id = ?
  AND team_id = ?
```

不能只靠主键；不能先按 task_id 查到再在 Python 里判断归属。越权查询统一表现为 `not_found`，不泄露“该 id 属于别人”。

### 3.4 不建议跨会话共享 Team

同一用户可同时开两个会话，如果共享任务板，会出现：

- 会话 A 的模型看到会话 B 的私密任务描述；
- 停止 A 可能影响 B 的 teammate；
- 上下文和产物归属混乱；
- 刷新一个页面时看到另一个会话的实时事件。

因此第一版坚持：

> **Team 永远绑定一个 session；同用户跨会话也不共享。**

将来若需要“个人长期任务板”，应作为独立功能重新设计授权和生命周期，不应偷偷复用 Team。

---

## 4. 数据模型与状态机

### 4.1 新增表

建议新增四张表，而不是把协作状态塞进 `subagent_runs` 的 JSON 字段。

```sql
CREATE TABLE team_runs (
    team_id         TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    generation_id   TEXT NOT NULL,
    lead_agent_id   TEXT NOT NULL,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL, -- active / completed / stopped / failed / stale
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    closed_at       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE team_tasks (
    task_id         TEXT PRIMARY KEY,
    team_id         TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    generation_id   TEXT NOT NULL,
    subject         TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL, -- pending / in_progress / completed
    owner_agent_id  TEXT,
    created_by      TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    version         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE team_task_deps (
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    generation_id   TEXT NOT NULL,
    team_id         TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    blocked_by      TEXT NOT NULL,
    PRIMARY KEY (team_id, task_id, blocked_by),
    FOREIGN KEY (team_id, task_id)
        REFERENCES team_tasks(team_id, task_id),
    FOREIGN KEY (team_id, blocked_by)
        REFERENCES team_tasks(team_id, blocked_by)
);

CREATE TABLE team_messages (
    message_id      TEXT PRIMARY KEY,
    team_id         TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    generation_id   TEXT NOT NULL,
    sender_agent_id TEXT NOT NULL,
    recipient_agent_id TEXT NOT NULL,
    kind            TEXT NOT NULL, -- note / result / request
    content         TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    delivered_at    INTEGER NOT NULL DEFAULT 0,
    read_at         INTEGER NOT NULL DEFAULT 0
);
```

索引至少包含：

- `(user_id, session_id, created_at)`
- `(team_id, status)`
- `(team_id, owner_agent_id)`
- `(team_id, recipient_agent_id, read_at)`
- `(user_id, session_id, generation_id, team_id, task_id)`

### 4.2 为什么依赖边单独建表

虽然第一版可以先用 JSON 存 `blockedBy`，但独立表更适合：

- 环检测；
- 上游完成后查询下游；
- 防止跨团队引用；
- 后续 Workflow 迁移；
- 不依赖 SQLite JSON 扩展。

第一版 3 个状态 + 1 张依赖表，成本可控。

注意：`team_task_deps` 也必须带完整作用域。若只存 `task_id / blocked_by`，依赖边就会成为绕过 scope 的侧门。SQLite 要真正启用复合外键，连接时需显式 `PRAGMA foreign_keys=ON`；否则必须在 repository 事务里做等价校验。

### 4.3 任务状态机

```text
pending ──claim──> in_progress ──complete──> completed
   ^                    │
   └────reclaim─────────┘
```

规则：

- `pending` 可以没有 owner；
- `in_progress` 必须有且只有一个 owner；
- `completed` 不再允许认领；
- 只有 lead 可以创建任务和指定 owner；
- teammate 只能认领本团队、自身可认领的任务；
- teammate 只能完成自己拥有的 in-progress 任务；
- lead 可以强制回收；
- 任务被回收后版本号递增；
- 已完成任务不能改回 pending，除非显式 reopen 且仅 lead 可做；
- 有未完成 `blockedBy` 的任务不能 claim；
- 依赖不得跨 team，不得自依赖，不得成环。

### 4.4 Team 状态机

```text
active ──正常结束──> completed
  ├──用户停止──────> stopped
  ├──不可恢复错误──> failed
  └──进程重启──────> stale
```

- `stale` 表示 live runtime 已经不存在，但历史可供查看；
- 启动时只把 streaming / running / in_progress 中确认无 live task 的状态收敛；
- 收敛必须按 `session_id + generation_id` 做，不能按 user 批量误杀活跃会话；
- 第一版不做自动恢复执行，只做显式“查看结果 / 重新发起”。

---

## 5. 工具权限：区分 lead、teammate、root

### 5.1 当前 P2 边界

P2 把所有编排工具都排除在子代理 registry 之外：

- `spawn_agent`
- `wait_agent`
- `read_agent_result`
- `list_agents`
- `close_agent`
- 预留的 `task_*` / `team_status` / `send_message`

这个原则正确，但 Team 需要把“协作工具”从“编排工具”中拆出来。

### 5.2 新分层

| 工具 | root/lead | teammate | 说明 |
|---|---:|---:|---|
| `spawn_agent` | ✅ | ❌ | 禁止递归编排 |
| `wait_agent` | ✅ | ❌ | teammate 不需要等自己 |
| `read_agent_result` | ✅ | ❌ | lead 读取 worker 结果 |
| `list_agents` | ✅ | ❌ | lead 查看花名册 |
| `close_agent` | ✅ | ❌ | lead 关闭 worker |
| `task_create` | ✅ | ❌ | 只有 lead 拆任务 |
| `task_get` | ✅ | ✅ | 只读本团队 |
| `task_list` | ✅ | ✅ | 只读本团队 |
| `task_update` | ✅ | ✅ | 受 owner / 状态机限制 |
| `team_status` | ✅ | ✅ | 只读本团队 |
| `send_message` | ✅ | ✅ | 第一版仅 lead ↔ teammate |

### 5.3 要改的能力定义

当前 `ORCHESTRATION_TOOLS` 是一个大集合。建议拆为：

```python
LEAD_ONLY_TOOLS = {
    "spawn_agent", "wait_agent", "read_agent_result",
    "list_agents", "close_agent", "task_create",
}
TEAMMATE_SAFE_TOOLS = {
    "task_get", "task_list", "task_update",
    "team_status", "send_message",
}
ORCHESTRATION_TOOLS = LEAD_ONLY_TOOLS | TEAMMATE_SAFE_TOOLS
```

组装时：

- root registry = 业务工具 + lead-only + teammate-safe；
- teammate registry = 业务工具 + teammate-safe；
- teammate-safe 工具内部仍必须再次校验身份，不能只靠“schema 里有”就放行。

### 5.4 工具内部身份校验

即使 teammate 有 `task_update`，也不能：

- 更新其他团队任务；
- 更新别人的 owner；
- 将别人的 in_progress 任务完成；
- 读取其他团队消息；
- 给其他团队发消息；
- 指定一个不存在的 recipient；
- 指定 lead 以外的广播地址。

每次调用从 `AgentContext` 取：

```text
user_id
session_id
team_id
generation_id
agent_id
parent_agent_id
```

禁止从工具参数里信任这些身份字段。

---

## 6. 运行时隔离

### 6.1 为什么不能只靠数据库隔离

DB 条件只能防止“读到别人的数据”。Team 还会消耗：

- 事件循环 CPU；
- asyncio 并发槽；
- 模型 API 连接和限流额度；
- MCP 连接；
- `code_run` 子进程；
- workspace 磁盘；
- SSE 推送带宽。

所以必须同时做逻辑隔离和资源隔离。

### 6.2 当前项目已有的隔离

已有较好基础：

- `AgentContext` 显式携带 user/session/message/generation/agent；
- 子代理独立 `AgentLoop` 和独立侧链；
- 子代理写入 `.subagents/<session>/<agent>`；
- `safe_workspace_path` 做 resolve 后包含性校验；
- `RuntimeHub` 按 `(user_id, model_id)` 缓存；
- 子代理 host 按 `(session_id, generation_id)` 注册；
- 停止接口按 session 校验用户后，再按 generation 级联；
- 子代理消息不写主 history。

### 6.3 当前需要修复的隐性风险

#### 风险 A：子代理复用父级同一个 MCP Client

`SubagentRuntime` 当前直接复用父 `ToolRegistry` 中的 MCP 工具，其闭包持有父 `fastmcp.Client`。多个 teammate 并发调用同一 Client 是否线程/任务安全，取决于 FastMCP transport 实现，不能假设一定安全。

第一版必须二选一：

1. 在用户级 toolset 上加串行锁，保证同一用户 MCP 调用不并发；
2. 或者为每个 teammate 建立独立 MCP toolset / Client。

推荐 MVP 用 1：**用户级 MCP 并发闸门**。理由是实现小、不会创建大量 stdio 进程；代价是同用户 teammate 的 MCP 调用串行，但不同用户互不影响。后续再按 server 建立独立连接池。

#### 风险 B：模型客户端被多个 teammate 共用

子代理复用父 `LLMClient`。HTTP 客户端本身通常支持并发，但用户 API 额度、限流和取消语义仍可能互相影响。

推荐：

- 每用户 / 每模型设置最大并发请求数；
- 同一用户超过额度时排队，不阻塞其他用户；
- 用户停止时只取消本 generation 的请求；
- 不把某个 teammate 的 429 自动升级成整个 Team 失败，允许 lead 将失败写入任务并继续其他任务。

#### 风险 C：一个用户的 `code_run` 拖垮事件循环

`code_run` 已经用 `asyncio.to_thread` + 子进程，但仍可能：

- 长时间占满 CPU；
- 创建大量进程；
- 写入大文件占满磁盘；
- 全局并发无限。

必须有用户级和全局级配额。

#### 风险 D：全局缓存 key 不完整

任何缓存如果只以 `session_id` 或 `team_id` 为 key，就可能串用户。建议所有 Team 相关缓存统一使用：

```python
TeamKey(
    user_id=...,
    session_id=...,
    generation_id=...,
    team_id=...,
)
```

不建议使用 `session_id` 单独作为全局 key，因为数据库中的 session id 虽然全局唯一，但一旦将来支持导入/迁移/多环境，单 key 会让隔离依赖偶然唯一性。

### 6.4 推荐资源闸门

MVP 至少增加：

```text
GLOBAL_MAX_ACTIVE_TEAMS       默认 16
USER_MAX_ACTIVE_TEAMS         默认 1
SESSION_MAX_ACTIVE_TEAMS      默认 1
USER_MAX_ACTIVE_SUBAGENTS     默认 4
TEAM_MAX_ACTIVE_SUBAGENTS     默认 4
USER_MAX_CODE_RUNS            默认 2
GLOBAL_MAX_CODE_RUNS          默认 8
USER_MAX_MCP_CALLS            默认 2（每用户）
```

规则：

- 申请顺序固定为 global → user → team；
- 任何一级失败必须释放已经拿到的 token；
- 等待配额要有超时；
- 超时返回可读错误，不能无限挂住；
- 用户停止时立即释放该 generation 的全部配额；
- 进程重启后内存配额清零，持久状态单独收敛为 stale。

---

## 7. 消息与任务协作协议

### 7.1 第一版只允许星型拓扑

```text
                lead(root)
               /    |    \
          worker1 worker2 worker3
```

- lead 可以给任意本团队 teammate 发消息；
- teammate 只能给 lead 发消息；
- teammate 之间不能直接通信；
- 不支持广播；
- 不支持把别的 teammate 拉进对话；
- 不支持 teammate 创建新 teammate。

星型拓扑是最稳健的第一步。它把消息路由和死锁面压到最低，也避免多个 agent 互相等待造成团队级卡死。

### 7.2 spawn 与任务绑定

`spawn_agent` 增加可选参数：

```json
{
  "task_name": "research_a",
  "message": "自包含任务...",
  "team_task_id": "task_xxx"
}
```

服务端行为：

1. 校验 `team_task_id` 属于当前团队；
2. 校验任务当前 `pending` 或由本次 lead 预分配；
3. 在同一事务中把任务 owner 设为该 teammate；
4. 子代理启动；
5. 如果 spawn 失败，任务回退为 `pending` 并清除 owner；
6. 如果 teammate 崩溃，任务回收为 `pending`，并记录错误消息。

### 7.3 teammate 完成任务

推荐两种模式：

- 绑定任务模式：teammate 完成时调用 `task_update(task_id, status=completed)`；
- 未绑定任务模式：第一版不建议，所有 teammate 都必须绑定 task。

强制绑定能避免：

- 任务漏关；
- 结果无法追溯；
- 多个 worker 对同一任务重复执行；
- lead 不知道哪个结果对应哪个产物。

### 7.4 消息投递语义

第一版采用“最多一次触发 + 可读审计”：

- 消息写入 DB 后立即计入审计；
- 发给正在运行的 teammate 时，进入其 inbox；
- teammate 空闲时，lead 的消息可以触发下一 turn；
- teammate 已在运行时不插入当前 turn，只排队；
- 读消息和标记 read 需要事务；
- 同一 message_id 重复 enqueue 必须幂等；
- 发送失败不能丢失待处理消息；
- 不承诺 exactly-once 工具执行，只承诺消息不重复入队。

不建议第一版做复杂的“已送达 / 已读 / 重试”三态组合。保留字段，语义先简单：

```text
created_at -> delivered_at -> read_at
```

### 7.5 不允许的事情

- teammate 直接 `send_message` 给另一个 teammate；
- 用未注册的字符串当 recipient；
- 用 `other_recipients` 做隐式广播；
- 通过 `task_update` 修改其他 owner 的任务；
- 通过 `task_list` 读取其他团队；
- 通过 `spawn_agent` 指定 `team_id` / `user_id`；
- 通过消息正文伪造系统指令来提权；
- 把外部不可信内容视为 lead 的授权。

最后一条尤其重要：Team 消息不是权限提升通道。即使 lead 说“用户已批准”，也不能绕过工具的参数校验、workspace 边界和 MCP 权限。

---

## 8. 并发与稳健性规则

### 8.1 所有写操作必须有 CAS

建议任务表增加 `version`，所有更新使用：

```sql
UPDATE team_tasks
SET status=?, owner_agent_id=?, version=version+1, updated_at=?
WHERE task_id=?
  AND user_id=?
  AND session_id=?
  AND team_id=?
  AND version=?
  AND status=?
```

影响行数为 0 时，返回：

- `claimed_by_other`
- `invalid_state`
- `not_found`

不能静默覆盖。

### 8.2 一致性原则

- 任务创建与依赖边写入同一事务；
- spawn 与任务 owner 绑定尽量同一事务；
- teammate 完成与任务完成同一事务；
- 消息入队与 ledger 去重同一事务；
- Team 结束与未完成任务回收同一事务；
- 任务回收和版本递增同一事务。

### 8.3 停止语义

用户停止一个 generation 时：

1. 校验 session 属于当前用户；
2. 只对本 generation 的 Team 发 stop；
3. lead 停止；
4. 所有 teammate 停止并取消；
5. 排队中的任务保留为 pending；
6. in-progress 任务回收；
7. Team 状态置 `stopped`；
8. SSE 发送最终状态；
9. 释放配额和 MCP / 子代理引用。

不得调用 `stop_all(user_id)` 这种用户级全停，除非产品后续明确提供“停止所有任务”入口。

### 8.4 失败语义

- 单个 teammate 失败：Team 不一定失败，lead 可将任务标 blocked / 重新分配；
- 所有 teammate 失败：Team `failed`；
- 排队超时：对应 spawn 失败，但 Team 可以继续；
- MCP 失败：作为工具观察返回，不默认杀死整个 Team；
- 配额超限：返回可读的 `capacity_exceeded`；
- 进程重启：live Team 置 `stale`，任务回 pending 或标记 interrupted；
- 用户停止：`stopped`，不再执行后续 turn。

### 8.5 防止一个用户影响其他用户

必须做到：

- 所有队列和信号量带 user 维度；
- 不允许全局无界队列；
- 每个用户最大活跃 Team / subagent / code_run / MCP 调用；
- SSE 每连接有背压策略；
- 单个用户的结果大小有上限；
- 消息正文和任务描述有字符上限；
- 事件缓冲有 per-agent 上限；
- 日志写入不阻塞事件循环；
- 大对象只返回引用或截断预览。

### 8.6 超时三件套

- `SUBAGENT_TIMEOUT_S`：单 teammate 墙钟；
- `TEAM_TASK_TIMEOUT_S`：单任务最长执行；
- `TEAM_IDLE_TIMEOUT_S`：团队无进展最长等待。

超时不是自动重试；超时后必须写清状态并释放资源。重试属于 Workflow 阶段。

---

## 9. 多实例与进程重启

### 9.1 MVP 的单实例边界

当前项目是单 FastAPI 进程 + SQLite，Team 的 live runtime 也在这个进程内。

因此第一版明确声明：

- `team_runs` 持久化可查询；
- `team_tasks` 持久化可恢复展示；
- `team_messages` 持久化可审计；
- 但正在运行的 teammate asyncio task 不跨进程恢复；
- 一个 generation 只能由一个实例持有。

### 9.2 不要伪造“跨实例继续运行”

如果没有以下机制，就不能声称支持跨实例：

- worker 租约；
- heartbeat；
- 任务队列；
- 幂等执行；
- 结果提交协议；
- 重试和去重。

所以第一版遇到服务重启时：

1. 启动时按 `generation_id` 查找没有 live host 的 Team；
2. 状态标 `stale`；
3. in-progress 任务 reap 为 pending；
4. 输出一条系统审计事件；
5. 前端显示“执行已中断，可重新发起”。

### 9.3 未来多实例演进

若以后要支持多实例，至少需要：

- DB 作为 Team/Task 的权威状态；
- Redis/DB 作为事件总线；
- worker lease + heartbeat；
- claim 使用数据库条件更新；
- 事件序列号单调；
- SSE 可通过事件表补帧；
- 每个任务有 `lease_owner`、`lease_expires_at`、`attempt`；
- 结果写回必须幂等。

这不是 Team-lite 的一部分，不要现在就半实现。

---

## 10. 前端与 SSE

### 10.1 Team 视图不是主对话覆盖层

前端应保持主对话和 Team 状态分离：

- 主消息流显示 lead 的最终答复；
- SubagentCard 继续显示单个 teammate；
- Team 面板显示任务板、成员、阻塞关系；
- 不把 teammate 的全部内部思考塞到主气泡；
- 刷新后可通过 API 拉取 Team 历史；
- 刷新不等于恢复运行中的 live team。

### 10.2 SSE 事件

建议在现有 subagent 事件之外新增：

```text
team_start
team_event
team_end
task_updated
team_message
```

字段必须包含：

- `team_id`
- `session_id`
- `agent_id`
- `task_id`（若适用）
- `status`
- `updated_at`

### 10.3 前端权限

前端只能展示当前用户当前会话的数据：

- Team API 必须经 `get_current_user`；
- 必须先在服务端验证 session ownership；
- 不允许仅凭 `team_id` 获取详情；
- 事件流应携带 session / generation 绑定；
- 不把“该 team_id 不存在”与“无权限”区分，统一 404。

### 10.4 回放

第一版建议：

- SSE 只负责实时；
- 任务板刷新通过 `GET /api/sessions/{session_id}/team`；
- 结果通过 `GET /api/sessions/{session_id}/team/tasks`；
- 不承诺关闭页面后自动续传未消费事件；
- 若需要回放，后续增加事件表 + cursor。

---

## 11. 安全模型

### 11.1 逻辑越权

所有 Team 工具必须拒绝：

- 跨用户 task_id
- 跨会话 task_id
- 跨团队 task_id
- 跨用户 message_id
- 伪造 sender
- 伪造 owner
- 伪造 recipient
- 伪造 agent_id
- 传入不存在的 blockedBy
- 传入其他团队的 blockedBy

### 11.2 Workspace

建议把团队共享产物目录固定为：

```text
backend/data/workspaces/<user_id>/.teams/<session_id>/<team_id>/
```

但 teammate 默认仍写自己的子目录：

```text
.../<team_id>/<agent_name>/
```

只有明确需要共享的文件才进入 team 根目录。这样既能协作，又不让多个 worker 默认覆盖同一文件。

### 11.3 Skill

- builtin / shared / user 三层继续沿用；
- teammate 只能看到 `ctx.user_id` 的私有 Skill；
- 不允许 Team 通过消息携带一个“全局 Skill 路径”；
- `load_skill` 仍校验 user_id；
- 公共 Skill 导入只清 runtime 缓存，不强制杀正在跑的 generation；正在运行的 Team 保留已构建的 prompt，下一轮才看到新共享技能。

### 11.4 MCP

- 每个用户只能使用自己的 MCP 配置；
- 不接受 teammate 指定其他 server_id；
- MCP 错误文本按普通工具结果返回；
- 不把 API key 写入 Team 消息、任务或结果；
- MCP 连接释放由 host 统一管理，不能被 teammate 自行关闭；
- 跨用户共享 Client 是禁止项。

### 11.5 Prompt 注入

Team 消息可能包含外部不可信内容。必须记住：

- 消息是数据，不是更高优先级指令；
- 只有用户输入和系统/开发者约束可以授权敏感动作；
- lead 转发的“用户已批准”不能作为权限提升凭据；
- 任务描述也不得绕过路径、MCP、Skill 边界。

---

## 12. 实施分层

### P3-A：Task Board 内核（先做）

目标：先把“共享事实”做稳，不接模型工具。

- 新增 `team_runs / team_tasks / team_task_deps`
- 任务状态机、CAS、owner、依赖
- 全部查询带 user/session/team
- 单测覆盖越权、并发 claim、阻塞、回收
- 提供内部 repository，不暴露 HTTP

验收：

- 两个用户、同一 task_id 风格输入互不可见；
- 两个并发 claim 只有一个成功；
- blockedBy 不满足时 claim 失败；
- complete 只允许 owner；
- 任务完成后依赖解锁；
- 停止后 in-progress 回收为 pending。

### P3-B：Team 运行时与工具

- `TeamContext` / `TeamHost`
- root `task_create`
- teammate `task_get / task_list / task_update / team_status`
- `spawn_agent(task_name, message, team_task_id)`
- `send_message`（仅星型）
- 能力拆分：lead-only / teammate-safe
- 任务 owner 与 subagent run 关联

验收：

- teammate 看不到 spawn / close；
- teammate 不能跨团队更新任务；
- teammate 不能直接给 teammate 发消息；
- spawn 失败回滚 task owner；
- teammate 失败可回收任务；
- lead 可以读取 worker 结果。

### P3-C：资源与前端

- 用户级 / 全局并发配额
- MCP 并发闸门
- `code_run` 用户级并发
- Team 面板
- SSE team/task/message 事件
- 刷新恢复展示
- 进程重启 stale 收敛

验收：

- 一个用户创建两个 Team 被拒绝或排队；
- 一个用户的 teammate 占满时，另一用户仍能正常执行；
- 停止 A 会话不影响 B；
- 刷新后任务板仍可读取；
- 重启后运行中 Team 显示 `stale`，不显示 running。

---

## 13. 测试矩阵

### 13.1 数据隔离

必须覆盖：

- 用户 A 读取用户 B 的 team → 404；
- 用户 A 用用户 B 的 task_id 更新 → not_found；
- 用户 A 用用户 B 的 session_id 创建 team → 404；
- 同用户不同 session 的 task_id 互不可见；
- 同用户不同 generation 的 live team 不可互相停止；
- 模型伪造 `user_id/session_id/team_id` 参数无效；
- 前端伪造 team_id 无效。

### 13.2 并发正确性

- 两个 teammate 同时 claim 同一任务，只有一个成功；
- claim 与 complete 并发，最终状态一致；
- lead 回收与 teammate 完成并发，不能双写；
- 删除任务与依赖查询并发，不出现悬挂边；
- 停止与 spawn 并发，不能留下 running task；
- 重复 message_id 只入队一次；
- 同一用户两个 generation 的 stop 不会误伤另一代。

### 13.3 资源隔离

- 用户 A 的 4 个 teammate 满载，用户 B 仍可 spawn；
- 用户 A 的 `code_run` 死循环不阻塞用户 B 的普通对话；
- 用户 A 的 MCP 超时不导致用户 B 的 MCP 超时；
- 全局配额满时返回明确错误，而不是静默挂起；
- 用户停止后释放配额，后续可以重新开始；
- 事件缓冲达到上限只影响本 teammate。

### 13.4 竞态与故障

- spawn 成功但任务 owner 更新失败 → 回滚；
- task 创建成功但依赖写入失败 → 回滚；
- teammate 进程内异常 → 任务回收并写错误；
- SSE 断开 → 不杀正在运行的 Team，但停止时仍能停止；
- 服务重启 → stale 收敛；
- 用户删除会话 → Team/任务/消息按所有权清理或归档；
- MCP 连接异常 → 只影响该工具调用。

### 13.5 端到端

至少两条真实 LLM E2E：

1. lead 创建 2 个任务，spawn 2 个 teammate，绑定任务，汇总结果；
2. 一个 teammate 失败，另一个成功，lead 输出部分失败结论，不把整轮标成功。

并增加一个双用户并发 E2E：

- A / B 同时运行 Team；
- 互相不可见；
- A 停止不影响 B；
- 两边的 SSE 都只收到自己的事件。

---

## 14. 关键设计核对

### 14.1 “多用户隔离”是否只做 user_id？

不是。必须同时满足：

- 租户隔离：`user_id`
- 会话隔离：`session_id`
- 运行隔离：`generation_id`
- 协作隔离：`team_id`
- 代理隔离：`agent_id`

### 14.2 “不打扰其他用户”是否只靠 try/except？

不是。需要：

- 每用户并发配额；
- 全局公平闸门；
- MCP / code_run 独立并发；
- 所有等待有超时；
- 所有取消只作用于自己的 generation；
- 事件、日志、结果有大小上限；
- 故障不通过共享可变全局状态传播。

### 14.3 Team 会不会影响 P2？

会有影响，但应保持兼容：

- 原有 `spawn_agent / wait_agent / ...` 保留；
- `spawn_agent` 增加可选 `team_task_id`；
- 不给子代理注入 lead-only 工具；
- 现有单机 subagent 测试不能改成必须创建 Team；
- 没有 Team 时，原有 subagent 行为不变。

### 14.4 是否需要先做 Workflow？

不需要。Team 先解决：

- 任务共享；
- owner；
- 依赖；
- 协作；
- 资源隔离；
- 任务回收。

Workflow 的 DAG / retry / resume / cache 可以后面在这套任务事实和资源闸门上继续搭。

### 14.5 是否要做 teammate 自动抢任务？

第一版不做。

原因：

- 空闲自驱会让消息、任务、turn、失败恢复同时复杂化；
- 容易产生无限空转；
- 用户很难预测 agent 何时会自己启动；
- 在 Web 多用户环境下会放大资源消耗。

第一版由 lead 显式创建并绑定任务，teammate 只执行绑定任务。

### 14.6 消息是否需要跨轮持久化？

需要持久化，但第一版不允许跨 generation 恢复 live 执行。

持久化的目的是：

- 刷新后能看到任务板；
- 审计谁发了什么；
- 任务失败后知道原因；
- 后续 Workflow 可以复用。

不是为了让 teammate 在请求结束后继续跑。

### 14.7 是否需要独立 Team 表？

需要。不要把 team 信息只塞进 `subagent_runs`：

- `subagent_runs` 是执行记录；
- `team_runs` 是协作容器；
- `team_tasks` 是共享事实；
- `team_messages` 是通信审计。

职责分开，才能避免以后 Workflow 复用任务板时被 subagent 生命周期卡死。

---

## 15. 风险与未决项

### 15.1 必须在本期解决

- 任务表四条件所有权；
- team_id 服务端生成；
- lead-only / teammate-safe 拆分；
- teammate 禁止递归 spawn；
- 单用户并发配额；
- 停止只作用于当前 generation；
- MCP 同用户并发闸门；
- 任务 owner 与 subagent 生命周期联动；
- 进程重启 stale 收敛；
- 双用户隔离 E2E。

### 15.2 可以延后

- teammate 自动抢任务；
- teammate 间通信；
- 跨会话 Team；
- 跨实例 live migration；
- 自动重试；
- 任务优先级和 SLA；
- 动态扩缩容；
- 完整 Workflow DAG。

### 15.3 需要产品确认

- 一个用户同时允许几个活跃 Team？
- 每个 Team 默认、最大几个 teammate？
- 单任务默认 / 最大执行时长？
- 用户停止后，任务是否保留可重新开始？
- team 历史是否需要长期保留？
- 公共 Skill 热更新时，运行中的 Team 是否要强制刷新 prompt？

推荐默认：

- 每用户 1 个活跃 Team；
- 每 Team 4 个 teammate；
- 单任务 10 分钟；
- 单 Team 30 分钟；
- 停止后任务保留为 pending；
- Team 历史保留 30 天；
- 公共 Skill 更新不杀运行中 Team。

---

## 16. 最终建议

### 16.1 实施顺序

1. 先写 `team_tasks` repository + CAS + 越权测试；
2. 再接 `TeamHost` 与工具；
3. 再加用户级资源配额；
4. 再做前端；
5. 最后才考虑 Workflow。

### 16.2 最重要的三条

1. **Team 不是全局对象，而是 user/session/generation 的从属对象。**
2. **任务板和消息的所有查询都必须带四条件所有权，不能只凭 id。**
3. **一个用户的 Team 必须有独立并发配额和故障域，不能靠 try/except 假装隔离。**

满足这三条后，再实现 lead/teammate 协作，系统才具备多用户 Web 服务所需的稳健性。

---

## 17. 方案复核后补充：隔离等级与必须修改点

> 本节是对前文的二次核对补充，实施时优先级高于前面的概括性描述。

### 17.1 明确隔离等级

这个系统有四种隔离，不能混为一谈：

| 等级 | 能力 | Team-lite 是否承诺 |
|---|---|---|
| L1 逻辑隔离 | 数据、身份、查询、工具参数 | 必须承诺 |
| L2 并发公平 | 每用户 / 全局配额、超时、取消 | 必须承诺 |
| L3 故障域隔离 | 一个 Team 失败不拖垮进程 / 其他用户 | 尽力承诺，需测试 |
| L4 OS 级隔离 | CPU / 内存 / 磁盘 / 系统调用硬隔离 | 不承诺，后续 worker/container |

当前是单 FastAPI 进程 + `asyncio` + SQLite：

- 可以做到 L1；
- 可以做 L2；
- 可以做大部分 L3；
- 不能仅靠当前架构做到 L4。

因此文档中的“不会影响其他用户”准确含义是：

> 不让一个用户的逻辑错误、越权、配额滥用、无限等待或 Team 失败传播到其他用户；不承诺恶意代码绝对无法消耗主机资源。

如果产品需要真正多租户安全，后续必须把 `code_run` 和 teammate 执行迁移到独立 worker 容器。

### 17.2 必须修改 `AgentContext`

现有：

```python
@dataclass(frozen=True)
class AgentContext:
    user_id: str
    session_id: str
    message_id: str
    generation_id: str
    agent_id: str = ROOT_AGENT_ID
    parent_agent_id: str | None = None
    depth: int = 0
```

Team 版本至少增加：

```python
team_id: str | None = None
team_role: str = "lead"   # lead / teammate
```

同时 `child()` 必须：

- 保留 `team_id`；
- 把 `team_role` 设为 `teammate`；
- 不继承 lead-only 权限；
- 不允许调用方覆盖 `user_id / session_id / generation_id / team_id`。

### 17.3 必须新增 Team 级工具上下文

不要每个工具单独从参数解析身份。建议统一提供：

```python
@dataclass(frozen=True)
class TeamScope:
    user_id: str
    session_id: str
    generation_id: str
    team_id: str
    agent_id: str
    role: str
```

所有 Team repository 方法接收 `TeamScope`，而不是裸 id。

### 17.4 必须明确 SQLite 并发设置

当前只有 `journal_mode=WAL`，没有明确 `busy_timeout`。Team 并发 claim / complete 会增加写竞争。

建议：

- 所有连接统一设置 `PRAGMA busy_timeout = 5000`；
- 写事务尽量短；
- 不在事务内调用 LLM / MCP / 文件 IO；
- claim 用单条条件 UPDATE；
- 捕获 `database is locked` 并返回可重试错误；
- 不把长时间网络请求包进 DB 事务。

### 17.5 必须明确 event / SSE 背压

一个 Team 的 teammate 事件可能很多。建议：

- per-agent 事件行数上限；
- per-team 总事件上限；
- 超限只保留最近 N 条 + 哈希摘要；
- SSE 连接断开不能反压整个运行时；
- `EventBuffer` 满时 drop 非关键事件，但 `team_end / task_updated` 必须保证投递；
- 结果只传摘要和引用，不把完整大文件塞进 SSE。

### 17.6 必须明确“失败不扩散”实现

不是单纯 `try/except`，而是：

- 每个 teammate task 必须被 `asyncio.Task` 包裹；
- 任何未捕获异常都要在 task done callback 中收敛；
- 不能让异常冒泡到主 SSE generator；
- 不能让一个 teammate 的 `CancelledError` 取消兄弟 task；
- 用户停止 generation 只取消该 generation 的 task；
- 所有 `finally` 都要幂等释放配额；
- 进程级全局字典只能通过带锁的 register / unregister 修改。

### 17.7 关键新增测试

在 P3-A 之前先补：

- `AgentContext.child()` 保留 `team_id`，但降级为 teammate；
- 子代理无法通过参数传入别的 `team_id`；
- 同用户两个 session 的 Team 不能互查；
- 一个 Team stop 不取消另一个 Team 的 task；
- `database is locked` 被转成可读可重试错误；
- `EventBuffer` 满时关键终态仍能送达；
- 一个 teammate task 抛异常，兄弟 task 继续或按策略收敛，不拖垮进程。

### 17.8 复核结论

前文的数据模型、工具边界和实施分层成立，但必须补上：

1. 明确 L1–L4 隔离等级；
2. `AgentContext` 增加 `team_id / team_role`；
3. 统一 `TeamScope`；
4. SQLite `busy_timeout` 与短事务；
5. SSE / event 背压；
6. task 级异常收敛；
7. 这些不是“优化项”，而是 Team-lite 上线前必须满足的稳健性条件。

---

## 18. 代码级复核：四级作用域、任务所有权、资源闸门缺口

> 日期：2026-11-06。状态：审计结论，实施前必须逐项关闭。
> 本节优先于前文概括性描述；若与前文冲突，以本节为准。

### 18.1 审计范围与判定口径

本次不是只审“设计是否漂亮”，而是逐项对照现有代码，回答三个问题：

1. 四级作用域是否在**每一次读写**中真实生效；
2. 任务 owner 是否能被并发请求错误覆盖；
3. 一个用户占满资源时，其他用户是否仍能获得服务。

判定口径：

- **满足**：生产路径已强制 owner + scope，且有并发/越权测试；
- **部分满足**：有字段或局部校验，但仍存在裸 ID、全局 key 或可绕过入口；
- **硬缺口**：当前实现与上线门槛直接冲突，必须修改后才能做 Team；
- **延后项**：不阻塞 Team-lite，但需保留演进接口。

### 18.2 现状对照表

| 能力 | 现状 | 判定 | 证据 / 影响 |
|---|---|---|---|
| 用户、会话 API 鉴权 | session 路由先校验 current user | 满足 | `routers/sessions.py` 多数入口调用 `db.get_session(session_id, user_id)` |
| `AgentContext` 身份链 | 已有 user/session/message/generation/agent | 部分满足 | 缺 `team_id / team_role`；子代理组装仍主要靠闭包 |
| `session_history` 所有权 | 表只有 `session_id`，读写按裸 session | 硬缺口 | 失去 user 条件，且 `get + set` 不是同一事务 |
| `subagent_runs` 所有权 | 建表有 user/session，查询列表带 owner | 部分满足 | `set_status/finish/get/append/load` 仍按裸 `run_id` |
| `subagent_messages` 所有权 | 只有 `run_id/seq` | 硬缺口 | 依赖父 run 的偶然唯一性，不能直接证明租户归属 |
| 同会话单活跃 generation | 未限制 | 硬缺口 | 两个请求可同时写同一 history，Team 可重复绑定 |
| stop 精确到 generation | 允许 `generation_id=None` 停整个 session | 硬缺口 | 与“只停本代”冲突 |
| live host key | `(session_id, generation_id)` | 部分满足 | 没有 user/team，依赖 session id 全局唯一 |
| 单 Agent subagent 上限 | `SubagentRegistry.max_active` | 满足局部 | 是每 host 上限，不是每用户或全局上限 |
| 用户/全局资源闸门 | 未实现 | 硬缺口 | 多 session 可绕过单 host 上限 |
| `code_run` 超时 | 有 60s 默认、600s 上限 | 部分满足 | 超时能返回，但取消等待线程不会杀死子进程 |
| MCP 并发控制 | 每个用户 runtime 持有自己的 toolset | 部分满足 | 同用户多 teammate 共享 Client，无并发闸门 |
| EventBuffer 容量 | 无界 `asyncio.Queue` | 硬缺口 | 慢客户端或高频 delta 可导致内存膨胀 |
| 子代理事件游标 | 每节点独立 `event_seq` | 硬缺口 | 多 agent 的序号不可比较，增量读取会漏/重 |
| session 删除清理 | 清 sessions/messages/session_history | 部分满足 | 不清理 subagent/Team 数据，会留下孤儿 |
| stale 收敛 | 全局扫描 running 状态 | 部分满足 | 单进程可接受，Team 阶段必须按 scope 判断 live host |

**结论：** 当前 P2 的基础是可复用的，但不能直接把 Team 建在上面。P3-A 之前必须先补 L1 所有权与 L2 闸门，不能等到 P3-C 再补。
### 18.3 四级作用域的最终契约

四级作用域不是四个名字，而是一条**不可跳过的授权链**：

```text
user_id
  └─ session_id（必须属于该 user）
       └─ generation_id（必须属于该 session，且当前唯一活跃）
            └─ team_id（必须属于该 generation）
                 ├─ agent_id（必须属于该 team）
                 └─ task_id / message_id（必须属于该 team）
```

#### 18.3.1 服务端身份来源

| 字段 | 唯一可信来源 | 禁止来源 |
|---|---|---|
| `user_id` | 认证中间件 `get_current_user` | 请求体、LLM 工具参数、消息内容 |
| `session_id` | URL + DB owner 校验 | 工具参数、team 元数据 |
| `generation_id` | 服务端创建并登记 | 前端指定旧值、模型参数 |
| `team_id` | 服务端生成 | 模型参数、任务文本 |
| `agent_id` | `AgentContext.child()` | 模型参数、recipient 字符串 |

任何代码拿不到完整链时，应拒绝执行，而不是“先按 id 查再补判断”。后者容易在 catch、缓存、异步回调里漏掉 owner 条件。

#### 18.3.2 `TeamScope` 统一对象

建议把散落参数收口为一个不可变对象：

```python
@dataclass(frozen=True)
class TeamScope:
    user_id: str
    session_id: str
    generation_id: str
    team_id: str
    agent_id: str
    role: Literal["lead", "teammate"]

    def task_filter(self) -> tuple[str, str, str, str]:
        return (self.user_id, self.session_id, self.generation_id, self.team_id)
```

约束：

- repository 只接受 `TeamScope`，不接受裸 `task_id + user_id` 拼接；
- `task_id/message_id` 作为**数据主键**传入，但与 scope 一起进入 WHERE；
- 任何缓存 key 至少包含四元组 `scope.key()`；
- stop、配额释放、事件游标都使用同一 scope，不允许一个入口用 `(session, generation)`、另一个入口用 `(generation, team)`。

#### 18.3.3 数据库必须显式保存 `generation_id`

前文任务 SQL 只列了 `user/session/team`，这是不够的。`team_tasks`、`team_messages`、`team_task_deps` 都应显式保存或从严校验：

- `team_runs`：已有 `generation_id`；
- `team_tasks`：增加 `generation_id`；
- `team_messages`：增加 `generation_id`；
- `team_task_deps`：增加 `team_id`，并通过同 team 的 FK/触发器或应用层事务校验；
- 子代理表：建议增加 `generation_id` 与 `team_id`（无 Team 时写空字符串）。

不能只靠“team_id 唯一所以可以反查 generation”来省略字段：查询会多一次、缓存会多一态，删除/归档时也更容易出现悬挂引用。

#### 18.3.4 标准查询条件

所有任务查询/更新必须采用四条件 + 数据主键：

```sql
WHERE task_id = ?
  AND user_id = ?
  AND session_id = ?
  AND generation_id = ?
  AND team_id = ?
```

所有消息查询/更新同理：

```sql
WHERE message_id = ?
  AND user_id = ?
  AND session_id = ?
  AND generation_id = ?
  AND team_id = ?
```

越权统一返回 `not_found`，不返回“属于别人”的提示。
### 18.4 任务所有权：CAS 是唯一合法变更方式

“任务有 owner 字段”不等于任务所有权正确。真正要保证的是：**任何 owner/status 变更都以已知版本为前置条件，且验证调用者身份。**

#### 18.4.1 所有权角色

| 角色 | 可做 | 不可做 |
|---|---|---|
| lead | 创建、预分配、回收、reopen、查看全队 | 越过 scope 操作其他 Team |
| teammate | 查看本队、认领符合条件的 pending 任务、完成自己的 in_progress 任务 | 修改 owner、完成别人的任务、读取其他 Team |
| root（无 Team） | 使用 P2 子代理编排 | 伪造 `team_id` 进入 Team 数据 |

建议状态机：

```text
pending -> in_progress -> completed
   ^           |
   |           +-- reclaim（lead 或超时回收）
   |           +-- release（owner 主动释放）
   +-----------+
```

#### 18.4.2 claim 的原子 SQL

禁止：

```text
SELECT task -> 检查 pending/owner -> UPDATE task
```

必须单条条件更新：

```sql
UPDATE team_tasks
SET status = 'in_progress',
    owner_agent_id = :caller_agent_id,
    version = version + 1,
    updated_at = :now
WHERE task_id = :task_id
  AND user_id = :user_id
  AND session_id = :session_id
  AND generation_id = :generation_id
  AND team_id = :team_id
  AND status = 'pending'
  AND owner_agent_id IS NULL
  AND version = :expected_version
  AND NOT EXISTS (
      SELECT 1
      FROM team_task_deps d
      JOIN team_tasks upstream ON upstream.task_id = d.blocked_by
      WHERE d.task_id = team_tasks.task_id
        AND upstream.status <> 'completed'
  );
```

判定：

- `rowcount == 1`：claim 成功；
- `rowcount == 0`：再查原因，只返回 `not_found / claimed_by_other / blocked / invalid_state / stale_version`；
- 只查原因时也必须带完整 scope。

#### 18.4.3 complete 的原子 SQL

```sql
UPDATE team_tasks
SET status = 'completed',
    version = version + 1,
    updated_at = :now
WHERE task_id = :task_id
  AND user_id = :user_id
  AND session_id = :session_id
  AND generation_id = :generation_id
  AND team_id = :team_id
  AND status = 'in_progress'
  AND owner_agent_id = :caller_agent_id
  AND version = :expected_version;
```

`lead` 不能冒充 teammate 完成普通任务；若产品允许 lead 代完成，应使用显式 `force_complete` 工具并记录审计原因，不复用普通 complete。

#### 18.4.4 reclaim / release 的原子 SQL

reclaim 只能由 lead 或调度器执行：

```sql
UPDATE team_tasks
SET status = 'pending',
    owner_agent_id = NULL,
    version = version + 1,
    updated_at = :now
WHERE task_id = :task_id
  AND user_id = :user_id
  AND session_id = :session_id
  AND generation_id = :generation_id
  AND team_id = :team_id
  AND status = 'in_progress'
  AND version = :expected_version;
```

release 由 owner 执行，WHERE 中额外要求 `owner_agent_id = caller`。两者都必须递增 version，避免与 complete 竞争后“后写覆盖先写”。

#### 18.4.5 spawn 与任务绑定事务

`spawn_agent(team_task_id=...)` 的顺序必须是：

1. 校验 scope；
2. 校验 task 仍为 pending 且无 owner；
3. 在同一短事务中 CAS 绑定 owner 为即将创建的 agent；
4. 提交后启动 asyncio task；
5. 启动失败则用新 version 做补偿回收；
6. 不允许先创建子代理再补 owner。

如果 DB 提交成功、进程在启动前崩溃，启动收敛逻辑应看到 `in_progress + owner` 但没有 live task，按 stale 规则回收，而不是永远显示运行中。
### 18.5 资源闸门：四层预算 + 可取消等待

#### 18.5.1 四层预算

前文只列了部分配额，实际实现应统一为四层：

```text
GLOBAL（全进程 / 全实例共享上限）
  └─ USER（单用户公平上限）
       └─ TEAM（单团队上限）
            └─ AGENT（单子代理上限）
```

建议配置：

```text
GLOBAL_MAX_ACTIVE_TEAMS=16
USER_MAX_ACTIVE_TEAMS=1
SESSION_MAX_ACTIVE_TEAMS=1
TEAM_MAX_ACTIVE_SUBAGENTS=4
USER_MAX_ACTIVE_SUBAGENTS=8
GLOBAL_MAX_ACTIVE_SUBAGENTS=64
USER_MAX_CODE_RUNS=2
GLOBAL_MAX_CODE_RUNS=8
USER_MAX_MCP_CALLS=2
GLOBAL_MAX_MCP_CALLS=32
```

说明：

- `SESSION_MAX_ACTIVE_TEAMS=1` 与“同一 session 只允许一个活跃 generation”共同生效；
- Team 配额是预留资源，不是“用完再检查”；
- 同一用户有多个 session 时，`USER_MAX_ACTIVE_SUBAGENTS` 必须共享；
- 所有数值以环境变量覆盖，缺省值写进 `config.py`，不在业务代码里散落魔法数。

#### 18.5.2 acquire / release 契约

```python
async def acquire(scope, kind, amount=1, timeout_s=...) -> Lease
```

必须满足：

- 固定顺序申请：global → user → session/team → agent；
- 任一级失败/超时，逆序释放之前拿到的 token；
- 返回的 lease 带 `released` 标记，`release()` 必须幂等；
- 调用方使用 `async with quota.acquire(...)`，不要手动散落 try/finally；
- 停止时先标记 scope 为 closing，拒绝新 acquire，再释放旧 lease；
- 一个 agent 的 fail 不得直接释放其他 agent 的 lease。

#### 18.5.3 各资源的闸门位置

| 资源 | 闸门位置 | 释放时机 |
|---|---|---|
| Team spawn | `TeamHost.spawn` 前 | Team end / close / spawn 失败 |
| Subagent task | `SubagentHost.spawn` 前 | subagent terminal / close |
| `code_run` | `_run_code_impl` 入口 | 进程结束 / 取消完成 |
| MCP call | `_call_tool_safe` 外包装 | 调用返回 / 超时 |
| LLM 请求 | LLM client 调用前 | 流结束 / 取消 |
| SSE 连接 | 合并事件队列创建时 | 连接断开 / 生成结束 |

MCP 和 `code_run` 不能用“每 host 上限”冒充用户上限。每个用户多个 session 同时调用时，仍必须共享同一条用户预算。

#### 18.5.4 `code_run` 取消必须升级为进程树终止

当前 `subprocess.run` 在 `asyncio.to_thread` 中执行。取消协程只会取消等待，线程和子进程会继续运行到超时（最多 600s）。必须改成：

- `asyncio.create_subprocess_exec`；
- 为每次执行登记 `ProcessLease(scope, generation_id, team_id, agent_id, proc)`；
- 用户 stop 时先 `terminate()`，宽限 2 秒后 `kill()`；
- Windows 用 Job Object 或 `taskkill /T /F` 终止整棵进程树；
- 取消路径也必须 `finally` 释放 `code_run` 配额；
- 写入临时脚本的路径必须在同一 finally 清理。

验收标准：

- 启动一个 `while True` 的 Python 子进程；
- 调用 stop；
- 5 秒内该 PID 不存在；
- 配额立刻可被同一用户下一次执行复用；
- 另一个用户的 `code_run` 不受影响。
### 18.6 生命周期：stop、删除、stale、事件游标

#### 18.6.1 同一 session 单活跃 generation

`send_message` 当前直接生成新 `generation_id` 并进入流式生成。Team 阶段必须改为：

1. 校验 session 属于 current user；
2. 检查该 session 是否已有活跃 generation；
3. 有则返回 `409 active_generation_exists`，附带当前 generation_id；
4. 无则原子登记 lease；
5. 流式生成 finally 释放 lease。

建议双保险：

- 进程内 `session_lock[session_id]` 防同进程竞态；
- DB `session_generations` / `session_leases` 表防重启与未来多实例竞态。

仅靠内存锁不够，因为重启后内存锁消失；仅靠 DB 行锁也不够，因为长时间流式生成不应占用 SQLite 写事务。

#### 18.6.2 stop 必须明确 generation

`request_stop(session_id, generation_id=None)` 当前会停整个 session 的所有 generation，这是 P2 兼容行为，但不能用于 Team。

新契约：

- `POST /sessions/{session_id}/stop` 必须要求 `generation_id`；
- 缺省返回 `400 generation_id_required`；
- 若确实需要“停止会话全部任务”，另开 `stop_session` API，并在审计日志中显式记录；
- stop 前先校验 user/session owner；
- `RuntimeHub` live host key 升级为 `(user_id, session_id, generation_id, team_id)`；
- stop 只匹配该四元组，绝不按 session 或 user 批量取消。

#### 18.6.3 stale 收敛

启动时不能无条件把全表 `starting/running` 改成 errored，而应按 scope 做：

```text
for each scope with nonterminal status:
    if no live host in this process:
        mark team stale
        move in_progress tasks back to pending (CAS)
        mark subagent runs interrupted
```

单实例 MVP 可用全局扫描表，但更新语句必须包含具体 `scope`，并且不能误处理其他实例的 live lease。未来多实例必须依赖 lease/heartbeat。

#### 18.6.4 EventBuffer 与 SSE 背压

当前 `EventBuffer` 与合并队列都是无界 `asyncio.Queue`。Team 阶段需要：

- `EventBuffer(max_events, max_bytes)`；
- 关键事件保留：`team_end / task_updated / subagent_end / error`；
- 非关键 `text_delta / thinking_delta` 可合并或丢弃；
- 队列满时不能阻塞 runtime 主流程；生产者采用 `put_nowait` + 丢弃计数；
- 达到上限时向前端发一条 `event_overflow` 摘要；
- SSE 慢客户端超时后关闭该连接，但不应自动停止 Team；
- 结果正文只发送摘要/引用，完整产物走 workspace API。

#### 18.6.5 事件游标必须是 Team 级单调序列

当前 `AgentNode.event_seq` 是每个子代理独立计数，`wait_agent(since_event_seq)` 会把多个 agent 的序号混在一起比较。正确模型：

```text
TeamEvent {
  team_id,
  event_seq,       # 全 Team 单调递增
  agent_id,        # 产生事件的 agent
  task_id,         # 可选
  event_type,
  payload,
  created_at
}
```

- `wait_agent` 返回并接收 Team 级 cursor；
- 每个 agent 可以有 local_seq 用于调试，但不能作为跨 agent 比较依据；
- 事件表/环形缓冲的重放、去重都以 Team cursor 为准；
- Team 结束后保留最后 cursor，重启后明确标记不可续传。

### 18.7 删除与数据生命周期

删除 session 必须按 owner 事务或 sagas 清理：

```text
sessions
messages
session_history
session_generations
subagent_runs
subagent_messages
team_runs
team_tasks
team_task_deps
team_messages
team_events（若落库）
```

约束：

- 删除前校验 user/session owner；
- 先停止该 session 的 live generation（显式 stop，不能直接删库留下后台任务）；
- 大批量清理分批执行，避免长时间锁库；
- 任一步失败要可重试，不能出现“父记录已删、子记录仍可查”；
- 若要保留审计，应使用 `archived_at` 或独立归档表，而不是让孤儿数据继续可访问；
- Team workspace 目录也要按 scope 清理，不能只删 DB。

### 18.8 现有代码的最小改造顺序

#### 阶段 A：先补所有权基础设施（不接 Team UI）

1. `AgentContext` 增加 `team_id / team_role`，默认 None；
2. 新增 `TeamScope`；
3. `session_history` 增加 `user_id`，所有接口改为 `(user_id, session_id)`；
4. `subagent_messages` 增加 `user_id/session_id/generation_id/team_id`；
5. `subagent_runs` 所有更新改为 scope-based；
6. `RuntimeHub` host key 改为四元组；
7. stop 接口要求 generation_id；
8. 同 session 单活跃 generation。

#### 阶段 B：资源闸门

1. 新增 `QuotaManager`；
2. 接入 spawn / subagent / code_run / MCP / LLM；
3. `code_run` 改异步子进程 + 进程树终止；
4. EventBuffer 加容量上限；
5. Team 级 event_seq。

#### 阶段 C：Team 数据与工具

1. `team_runs/team_tasks/team_task_deps/team_messages`；
2. CAS repository；
3. lead-only / teammate-safe 工具；
4. spawn 与 task 绑定；
5. stale / 删除 / 前端。

**顺序不能倒。** 如果先做 Team 工具再补所有权，后续每个工具都要回填 scope，风险更高。
### 18.9 上线前 Go / No-Go 清单

以下项目全部为 **No-Go 阻塞项**，完成后才能进入 Team-lite 真实开发：

#### 作用域与所有权

- [ ] `session_history` 有 `user_id`，所有读写带 `(user_id, session_id)`；
- [ ] `subagent_runs/subagent_messages` 所有读写带完整 scope；
- [ ] `TeamScope` 不可伪造，工具参数不接受 user/session/generation/team；
- [ ] `team_tasks/team_messages` 显式保存 `generation_id`；
- [ ] 所有 Team repository 查询使用四条件 + 数据主键；
- [ ] 越权统一返回 `not_found`，不泄露其他 scope 是否存在。

#### 并发与生命周期

- [ ] 同一 session 最多一个活跃 generation；
- [ ] stop 必须指定 generation，不存在默停整个 session 的 Team 路径；
- [ ] `RuntimeHub` live host key 包含 user/session/generation/team；
- [ ] start / stop / spawn / complete 之间没有“先读后写”覆盖；
- [ ] session 删除清理全部子数据，且不会留下可访问孤儿；
- [ ] stale 收敛只处理无 live host 的具体 scope。

#### 资源闸门

- [ ] 每用户 / 全局 subagent 配额有效；
- [ ] 每用户 / 全局 `code_run` 配额有效；
- [ ] 每用户 MCP 并发闸门有效；
- [ ] `code_run` 取消后真实子进程树被终止；
- [ ] EventBuffer 有条数 + 字节上限；
- [ ] Team 级事件游标全局单调；
- [ ] 所有 acquire 有超时，所有 release 幂等。

#### 公平性与故障域

- [ ] 用户 A 占满自己的配额，用户 B 仍可正常创建 Team / 子代理；
- [ ] 用户 A 的 `code_run` 死循环不影响用户 B；
- [ ] 用户 A 的 MCP 超时不影响用户 B；
- [ ] 一个 teammate 异常不会取消兄弟 teammate；
- [ ] A 停止只停止 A 的 generation/team；
- [ ] 双用户并发 E2E 通过。

### 18.10 新增测试清单（按层）

#### 18.10.1 作用域单元测试

- `HistoryStore.get/set/append` 用用户 A 的 session 访问用户 B 时得到空/拒绝；
- `subagent_store.set_status/finish/append/load/get` 全部传错 user/session/generation 时 0 行；
- `TeamScope` 序列化后 `user_id/team_id` 不被工具覆盖；
- 伪造 `team_id`、`task_id`、`message_id` 均不可读取。

#### 18.10.2 CAS 并发测试

- 两个协程 claim 同一 task，只有一个 `rowcount=1`；
- claim 与 complete 同时执行，不出现 completed 又被 pending 覆盖；
- reclaim 与 complete 竞争时，版本较旧者失败；
- blockedBy 未完成时 claim 失败，上游 completed 后成功；
- 依赖跨 team 插入失败；
- task 删除与依赖查询并发不产生悬挂边。

#### 18.10.3 资源闸门测试

- 用户 A 开满 `USER_MAX_ACTIVE_SUBAGENTS`，B 仍能 spawn；
- 用户 A 开多个 session，仍共享同一用户配额；
- 全局配额满时请求在 timeout 内返回 `capacity_exceeded`；
- 停止 generation 后 token 计数归零；
- 重复 `release()` 不产生负计数；
- `code_run` 被取消后无残留进程；
- EventBuffer 满后关键终态仍送达。

#### 18.10.4 端到端测试

1. lead 创建两个任务并绑定两个 teammate，最终汇总；
2. 一个 teammate 失败，另一个成功，Team 不整体误报成功；
3. 用户 A / B 同时运行 Team，双方 SSE 不串流；
4. A stop 后 B 继续运行；
5. 服务重启后 running Team 变 stale、任务回 pending；
6. 删除 session 后所有 Team/subagent 数据不可查询。

### 18.11 核实后的最终结论

前文方案总体成立，但必须把以下认知升级为铁律：

1. **四级作用域是写入契约，不是注释。** `generation_id` 必须出现在 Team 表和 WHERE 条件里。
2. **任务所有权靠 CAS，不靠 owner 字段。** 所有变更必须带 expected version 和调用者身份。
3. **资源闸门分四级。** 只做每 host 上限无法满足 Web 多用户公平性。
4. **停止必须精确到 generation。** 不能用 session 作为“顺便停止全部”的隐式范围。
5. **取消必须落到进程。** 协程取消不等于 `code_run` 子进程终止。
6. **事件必须有界且 Team 级有序。** 无序游标会造成事件丢失，最终影响任务事实判断。
7. **L4 暂不承诺。** 当前是逻辑隔离和资源配额，不是 OS/容器级硬隔离；`code_run` 的 CPU、内存、系统调用沙箱仍是后续独立工作。

满足 §18.9 全部阻塞项后，才建议开始 P3-A 的 Team Task Board 真实实现。

---

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-06 | 初版：GA CLI 单用户假设审计、Web 多用户四级作用域、Team 数据模型、工具权限、资源隔离、稳健性规则、实施分层与测试矩阵 |
| 2026-11-06 | 二次核对：增加 L1-L4 隔离等级、AgentContext/TeamScope、SQLite 并发、SSE 背压、失败不扩散与补充测试 |
| 2026-11-06 | 三次代码级复核：补 §18，核实 session_history/subagent/stop/RuntimeHub/code_run/MCP/EventBuffer 缺口，修正四级任务 SQL，新增 CAS、四层资源闸门、Go/No-Go 清单与测试清单 |
