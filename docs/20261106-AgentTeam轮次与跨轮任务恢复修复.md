# Agent Team 轮次上限与跨轮任务恢复修复

> 日期：2026-11-06  
> 状态：已实施，待真实浏览器回归  
> 范围：Subagent 轮数参数链路、Team 任务板跨 generation 恢复、主轮结束后的 teammate 生命周期、前端 Team 快照合并

## 1. 背景与真实现象

Agent Team 实测中，writer_code 在 24 轮后停止，reviewer_code 启动约 4 秒后以 `turns=0` 停止。随后用户继续输入时，会话创建了新的 Team，上一代仍处于 `pending` 的 review 任务没有出现在新任务板中。

这次排查确认了两类独立问题：

1. **轮数问题**：24 不是服务端把 40/45 “向下截断”为 24，而是 `spawn_teammate` 链路根本没有把 `max_turns` 参数传到 `SubagentRuntime`，最终一直使用旧的默认值 24。
2. **生命周期问题**：Team 运行时按 generation 隔离。主 Agent 已结束而 teammate 仍在运行时，SSE 合并器只再排空约 200ms，随后 `close_all()` 会终止 teammate。任务板在下一代创建新 Team 时也没有恢复，因此看起来像“团队上下文反复重置”。

数据库复核还发现，Hlaudz 所说的“7 个团队”并不准确：问题会话当时实际存在 3 个 Team。但“每轮新建 Team、旧任务板不延续、活 teammate 被主轮结束误杀”这三个事实成立。

## 2. 修复一：统一默认轮数并打通参数

调整如下：

| 位置 | 修复前 | 修复后 |
|---|---:|---:|
| `SubagentRuntime` 默认值 | 24 | 300 |
| `AGENT_MAX_TURNS` / 代码默认值 | 主 Agent 300，子代理 24 | 主/子 Agent 均以 300 为默认 |
| `spawn_teammate` schema | 无 `max_turns` | 支持 `max_turns` |
| `team_tools -> TeamHost -> session -> SubagentHost -> SubagentSpec` | 参数被丢弃 | 全链路透传 |

优先级：

```text
spawn_agent / spawn_teammate 的 max_turns
  > SUBAGENT_MAX_TURNS
  > AGENT_MAX_TURNS
  > 代码默认 300
```

`max_turns` 只是防止异常循环的硬上限，不是目标轮数；正常任务只要模型返回无工具调用的最终消息就会结束。

## 3. 修复二：跨 generation 恢复任务板

Team 的 live runtime 仍然绑定单一 generation，这是多用户隔离和停止级联的安全边界；但任务板可以跨用户的多条消息延续。

新 Team 创建时，会在同一个 SQLite `BEGIN IMMEDIATE` 事务中：

1. 在同一 `user_id + session_id` 内，查找最近一个仍含有 `pending / in_progress` 任务的历史 Team；
2. 为该 Team 的每个任务生成新的 `task_id`，不修改旧 Team 的审计快照；
3. 将 `in_progress` 恢复为 `pending`，清空 owner，version 重置为 0；
4. 按旧任务 ID 到新任务 ID 的映射重建 `blocked_by`；
5. 复制最近最多 200 条团队消息；
6. 写入 `team_resumed` 事件，payload 携带来源 Team、来源 generation、任务与消息快照。

关键边界：

- 只查同一用户、同一会话；跨用户和跨会话绝不复制；
- 只有存在未完成任务时才恢复，已完成/已取消的纯历史板不会无限跟随每一次新消息；
- 查找源 Team 时使用“最近一个仍有 open task 的 Team”，因此后续创建过空 Team 也不会把更早的未完成任务遮蔽；
- 旧 Team 的任务和消息保持原样，便于审计。

前端收到 `team_resumed` 后，将 payload 中的任务和消息合并到当前 TeamSnapshot，首次打开面板即可看到恢复后的任务板。

## 4. 修复三：主轮结束后的 teammate 收尾

新增 `SubagentHost.has_active()`，并改造 SSE 事件合并器：

- 主 Agent 已返回最终消息时，不立即关闭子代理事件缓冲；
- 若有 teammate 仍在运行，继续通过同一条 SSE 转发 `subagent_event / subagent_end`；
- 默认最多等待 `TEAM_DRAIN_TIMEOUT_S=300` 秒；
- 用户点击停止时，仍由 `/stop` 的取消链路立即打断；
- 超时后主动调用 `stop_all()`，再由 finally 的 `close_all()` 做有界回收。

这并不表示 teammate 能跨 HTTP 请求常驻。请求真正结束后，live teammate 仍不可恢复；可恢复的是持久任务板、消息和事件。下一轮由 Lead 根据恢复的任务板重新派发执行。

## 5. 测试与验证

已完成：

```text
backend: 134 passed, 1 warning
frontend: npm run build 通过
```

新增回归覆盖：

- `spawn_teammate(max_turns=45)` 透传到 `TeamHost`；
- 未完成 Team 在下一代恢复，任务 ID 重新生成；
- `in_progress -> pending` 且 owner 清空；
- `blocked_by` 按新任务 ID 重映射；
- 团队消息按 scope 复制；
- 跨用户不恢复，纯 completed/cancelled 板不恢复；
- 主 Agent 结束后，`_merged_events` 会等待仍活跃的 child 并转发 `subagent_end`。

## 6. 未决项与风险

1. Team 仍是 generation 级 live runtime，不能把上一请求的 asyncio teammate 迁移到新请求；
2. 任务板恢复是快照复制，不合并同一任务在多代中的多次编辑；CAS version 会在新 Team 中重置；
3. 消息最多复制 200 条，避免长会话把无界历史带进新 Team；
4. `TEAM_DRAIN_TIMEOUT_S` 是保护阈值，超时会停止仍未结束的 teammate，而不是伪造完成；
5. 300 轮仍可能被上游网络错误、模型协议错误或用户停止提前终止，应结合重试与错误提示继续改进。

## 7. 变更文件

- `backend/app/team_resume.py`
- `backend/app/team_store.py`
- `backend/app/subagent/runtime.py`
- `backend/app/subagent/session.py`
- `backend/app/subagent/host.py`
- `backend/app/team_tools.py`
- `backend/app/team_host.py`
- `backend/app/routers/chat.py`
- `backend/tests/test_team_store.py`
- `backend/tests/test_team_tools.py`
- `backend/tests/test_subagent_chat.py`
- `frontend/src/App.tsx`
- `frontend/src/components/TeamPanel.tsx`
- `README.md`

## 8. 后续建议

优先用真实浏览器复测以下场景：

1. Lead 创建 A、B 两个依赖任务，A 完成后在下一轮继续；
2. writer 完成但 review 仍 pending，再发一条消息，确认新任务板仍能看到 review；
3. Lead 先返回最终文本、reviewer 仍在运行，确认前端继续显示 reviewer 动态，而不是立即 stopped；
4. 点击停止，确认 `task_update`、子代理和 code_run 都被级联终止。
