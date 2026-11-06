# Agent Team P3 实施记录（Web 多用户隔离基线）

> 摘要：P3 已完成 Team-lite 基线——持久任务板、受控消息、teammate 生命周期、四级作用域、CAS 所有权、资源闸门、SSE 任务面板与真实 LLM E2E 均已落地。
> 日期：2026-11-06
> 状态：已完成（基线）
> 设计文档：`docs/20261106-AgentTeam-Web多用户隔离设计方案.md`

## 1. 交付范围

- 新增 `user_id -> session_id -> generation_id -> team_id` 四级作用域，所有 Team 读写强制带完整 scope。
- 新增持久表：`team_runs`、`team_tasks`、`team_task_deps`、`team_messages`、`team_events`、`session_generations`。
- Root 即 lead；teammate 继承业务工具（file / code_run / Skill / MCP），但不能调用编排工具、不能递归 spawn。
- Team-lite 工具：`team_status`、`task_create`、`task_get`、`task_list`、`task_update`、`send_message`、`list_messages`、`spawn_teammate`。
- 任务支持 `blocked_by` 依赖、owner 软锁、CAS `expected_version`、失败 spawn 自动 reclaim。
- 一个会话同一时刻只允许一个 active generation；active Team 生命周期租约覆盖整轮生成。
- 停止级联到所有 teammate，并接入 `code_run` 进程注册表。
- 前端新增右侧 `TeamPanel`：任务 / 消息 / 动态三视图，SSE 增量更新、刷新走 REST 恢复。

## 2. 关键稳健性修复

### 2.1 Windows 热加载下的 code_run

Uvicorn 0.41 在 Windows `--reload` 模式使用 `SelectorEventLoop`，该 loop 不实现
`asyncio.create_subprocess_exec()`。修复为：

- 新增 `spawn_process()` 适配层；
- Proactor loop 继续使用原生 asyncio subprocess；
- Selector loop 使用 `subprocess.Popen + asyncio.to_thread` 兼容实现；
- Windows 进程树终止统一通过 `taskkill /PID ... /T /F`；
- 异常输出改为 `TypeName: detail`，不再出现空的 `[工具执行错误]`。

### 2.2 执行身份透传

`build_builtin_tools()` 的 `code_run` 现在把 `session_id / generation_id / agent_id`
传给 `_run_code_impl()`。这保证 root、subagent、teammate 的进程租约都能按正确
generation 归属，停止时不会漏杀或误杀其他代。

### 2.3 Team 生命周期租约

`prepare_turn_team()` 在创建 Team 失败时会显式 `release_lifecycle()`，避免长租约泄漏后
阻塞同一用户的下一轮 Team。错误事件也保留具体异常类型，便于排查。

### 2.4 prompt 协作语义

Root / child prompt 已明确：

- `spawn_teammate(team_task_id=...)` 的绑定动作已完成认领，teammate 不再 claim；
- teammate 用 `task_get` 拿到 version 后自己 complete；
- lead 不得在 teammate 运行时 reclaim/接管，只有确认 errored/stopped 后才回收；
- teammate 通过 `send_message` 汇报，不自由广播。

## 3. 测试与验收

### 3.1 自动化测试

| 项目 | 结果 |
|---|---|
| 后端全量 `python -m pytest -q` | 118 passed，1 warning |
| 前端 `npx tsc --noEmit` | 通过 |
| 前端 `npm run build` | 通过（大 chunk warning 保留，非本次引入） |
| `git diff --check` | 通过 |

覆盖重点：

- Team scope 跨用户 / 跨会话不可见；
- 任务 CAS、依赖阻塞、owner 校验；
- teammate 不能 create / spawn / 读取他人 inbox；
- spawn 失败回收任务；
- Team lifecycle 租约显式释放；
- code_run 身份透传到进程注册表；
- stop 级联与进程树终止。

### 3.2 真实 LLM E2E：成功路径

配置：`test / 12345678`，模型 `deepseek-v4.1-flash`。

结果：

- Team 最终状态：`completed`；
- 两个 task 均为 `completed`；
- 两个 task owner 分别为 `sub:calc_17x23`、`sub:calc_19x29`；
- 两个 teammate 都真实调用 `code_run`，分别验证 391 / 551；
- 两个 teammate 都通过 `send_message` 向 lead 汇报；
- 主 SSE 事件计数：2 `subagent_start`、2 `subagent_end`、1 `done`。

### 3.3 真实 LLM E2E：停止路径

在两个 teammate 已启动后调用 stop：

```json
{"ok": true, "stopped": 1, "subagents_stopped": 2, "message": null}
```

数据库最终状态：

- Team：`stopped`
- 两个 teammate：`stopped`
- generation：`finished`（已释放）
- 两个未完成任务保留为 `in_progress`，没有伪造 completed

说明：本次模型在真正进入 `time.sleep(120)` 的 `code_run` 之前就被停止，因此真实 E2E 证明的是
generation/Team/subagent 级联；进程树终止由 `test_code_run_stop_kills_child` 单元测试覆盖。

## 4. 已知边界

- Team 只在单个 generation 内 live；刷新可恢复任务/消息/事件，不能恢复已结束的 live teammate。
- 当前是单实例进程内配额和 stop registry；未来多实例部署需要迁移到 DB 租约 / Redis 等共享协调层。
- 任务依赖只做直接阻塞，不做自动调度器；lead 仍负责显式 spawn / wait / 汇总。
- 未做 teammate 间自由通信、跨用户/跨会话 Team、后台常驻 Team。
