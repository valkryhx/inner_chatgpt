# P2 changelog：Subagent 进程内 asyncio 子代理运行时

> 日期：2026-11-05
> 前置：`docs/20261105-changelog-SkillP1.md`（Skill + 基础工具已完成）
> 设计：`docs/20261105-Subagent调研与设计方案.md`（S1~S6 已按此实施）

## 1. 一句话结论

在**不引入 langchain / langgraph、不 import GenericAgent** 的前提下，用自研内核实现了
「进程内 asyncio 子代理运行时」：root agent 可 `spawn_agent` 派生旁路子代理，`wait_agent`
等待、`read_agent_result` 读回权威结果、`close_agent` 释放槽位；子代理事件与主代理事件
合并进**同一条 SSE**，前端以可折叠卡片展示；停止、超时、级联、槽位、重名、写根隔离、
独立侧链 history 全部落地。

**关键取舍**：不做 GA 的「一个子代理一个 OS 进程」。10 用户 × 3 子代理 = 30 个进程的
内存/启动成本不可接受，而子代理瓶颈是 LLM 网络 IO 而非 CPU，协程足够。

## 2. 已完成清单（S1~S6）

### S1 骨架与持久化

- 新包 `backend/app/subagent/`（11 个模块，见 §3）。
- `AgentContext`（frozen dataclass）：`user_id / session_id / message_id / generation_id /
  agent_id / parent_agent_id / depth`；`child(name)` 派生 `sub:<name>` 并 `depth+1`。
  所有写文件 / 写库 / 调模型 / 读技能都从 ctx 取 user_id，不读模块级全局。
- 两张表（`backend/app/subagent/store.py`）：
  - `subagent_runs`：一次子代理运行的身份 + 状态 + 结果 + `started_at/finished_at`；
  - `subagent_messages`：子代理侧链消息（按 `run_id + seq` 追加，增量写入）。
  - 索引：`(session_id, started_at)`、`(user_id, session_id)`。
- 进程启动时 `init_tables()` + `mark_stale_running_as_errored()`：上次崩溃残留的 `running`
  统一置为 `errored("restarted")`，避免僵尸状态。

### S2 Registry + Runtime

- `SubagentRegistry`：四条护栏全部实测——`max_depth`（默认 2）、`max_active`（默认 4，
  **每 host 即每 session，不是全局**）、重名拒绝、idle 淘汰；`validate_task_name()` 拒绝
  非法字符与保留名（`root/main/team-lead/user/system`）。
- `SubagentRuntime`：复用父 `LLMClient`（不新建连接池）；system prompt 由
  `build_child_system_prompt(skill_block)` 组装；registry 从父 registry 继承**全部业务工具**
  （`file_read/file_write/file_patch/list_files/code_run/load_skill` + 全部 MCP），
  只剔除编排工具；写类 builtin 工具替换为带隔离根的版本
  （`write_root=.subagents/<session>/<name>`）。
- 初始 history 默认只有一条 user message（`fork_turns=none`，上下文隔离）；
  可选 `all` / `recent:N`（只继承 user/assistant 纯文本，不继承半截 tool_calls）。

### S3 Host + 5 个编排工具

- `SubagentHost`：`spawn / wait / read_result / list_agents / close / close_all / stop_all`。
- `tools.py` 暴露 5 个 Tool：`spawn_agent` / `wait_agent` / `read_agent_result` /
  `list_agents` / `close_agent`，只在 root 的 registry 里存在。
- 子代理事件经 `EventBuffer`（单队列）扇出；`_merged_events()` 在 SSE 层合并主 + 子事件，
  顺序即真实发生顺序。

### S4 停止 / 超时 / 级联

- `stop_generation` 接口级联 `hub.stop_subagent_generation()`：主 stop → 所有子代理
  `stop.set()` + `task.cancel()`，并返回 `subagents_stopped` 数量。
- 超时三兄弟区分清楚：`wait_agent.timeout`（**不杀子**，返回 `timed_out + remaining_targets`）、
  墙钟 `SUBAGENT_TIMEOUT_S`（默认 300s，超时置 `stopped` 并记 error）、`max_turns`（AgentLoop 轮数）。
- `wait_agent.since_event_seq`：增量事件游标（本轮补齐，见 §5）。

### S5 前端

- `types.ts`：新增 `SubagentEventItem` / `SubagentBlock`，`MessageBlock` 联合类型加 `SubagentBlock`；
  `StreamEvent` 加 `subagent_start` / `subagent_event` / `subagent_end`。
- `components/SubagentCard.tsx`：可折叠卡片（名字 / 角色 / 状态 / 工具数 / 轮数 + 展开时间线 + 结果区）。
  为兼容「结果与末尾文本重复」做了**仅末尾完全相同去重**的收敛（不完全相同则都显示）。
- `MessageList.tsx`：`renderBlocks` 增加 `subagent` 分支，沿用「按 blocks 顺序渲染」原则，
  **不打乱既有 reasoning / tool / text 的顺序**（本轮未改动既有过滤逻辑，`.` 仍原样显示）。
- `App.tsx`：`onEvent` 处理 3 个 subagent 事件，按 `agent_id` 汇入对应卡片。
- `styles/index.css`：追加 `.subagent-card` 全套样式（附录在文件末尾，不覆盖既有样式）。

### S6 提示词接入与文档

- `ROOT_AGENT_HINT`（`subagent/prompts.py`）已接入主 system prompt：
  `UserRuntime.build()` 里改为 `SYSTEM_PROMPT + skill_block + "\n\n" + ROOT_AGENT_HINT`。
  该段**只进 root**；子代理走 `build_child_system_prompt()`，看不到编排规则，也不会套娃。
- 本 changelog + `AGENTS.md` §0.1/§0.2 更新 + 设计文档 §16 变更记录同步。

## 3. 代码结构

```
backend/app/subagent/
  __init__.py      # 导出 AgentContext
  context.py       # AgentContext（多租户身份贯穿）
  store.py         # subagent_runs / subagent_messages 两张表 + CRUD + 侧链增量写
  events.py        # StopSignal + EventBuffer（统一队列）
  capabilities.py  # ORCHESTRATION_TOOLS / READ_ONLY_TOOLS / 闭世界裁剪
  registry.py      # AgentNode + SubagentRegistry（深度/槽位/重名/淘汰 + 事件游标）
  prompts.py       # ROOT_AGENT_HINT / SUBAGENT_HINT / 4 个角色提示词
  runtime.py       # SubagentRuntime + SubagentSpec（写根隔离、闭世界 registry）
  host.py          # SubagentHost（生命周期核心）
  tools.py         # 5 个编排 Tool
  session.py       # build_turn_runtime（每轮派生 TurnRuntime）
```

改动文件：

| 文件 | 改动 |
|---|---|
| `workspace.py` | `write_root_for()`；`safe_workspace_path(..., root="")` 支持子根 |
| `builtin_tools.py` | `build_builtin_tools(user_id, *, write_root=None)`；写类工具加 `root` |
| `runtime.py` | `RuntimeHub` 增加 `_subagent_hosts` + register/unregister/get/stop + `shutdown()` 先关 host |
| `routers/chat.py` | `_merged_events()`、`_TurnUR`、每轮 `build_turn_runtime`、SSE 累积 subagent block、stop 级联 |
| `main.py` | lifespan 里 `init_tables()` + `mark_stale_running_as_errored()` |
| `agent.py` | 主 system prompt 追加 `ROOT_AGENT_HINT` |
| 前端 5 个文件 | 见 §2 S5 |

## 4. 测试与验证（2026-11-05）

### 4.1 单元测试

`cd backend && python -m pytest tests -q` → **62 passed**（P1 结束时 42 项，本轮新增 20 项）。

| 文件 | 覆盖 | 数量 |
|---|---|---|
| `test_subagent_store.py` | context / store / events | 7 |
| `test_subagent_capabilities.py` | 能力继承（含 code_run/Skill/MCP）+ 编排工具剥离 / registry 护栏 | 9 |
| `test_subagent_host.py` | spawn→wait→read→close / 无编排工具 / wait 超时不杀子 / stop_all / 槽位 / 重名 / since_event_seq 增量 | 7 |
| `test_subagent_chat.py` | turn runtime / merged events / SSE subagent block / stop 级联 | 4 |

### 4.2 前端构建

`cd frontend && npm run build` → 通过（仅有既存的 chunk >500KB 体积警告，非本轮引入）。

### 4.3 真实 LLM E2E（`test / 12345678` + `deepseek-v4.1-flash`）

| 轮次 | 会话 | 关键结果 |
|---|---|---|
| #1 | `co_f0169b89f669` | `subagent_start`×2 / `subagent_end`×2 / `subagent_event`×327；`spawn_agent`×2 → `wait_agent` → `read_agent_result`×2 → DONE；2 张卡片，侧链 events 161 / 166 |
| #2（修复后） | `co_7b9cd2d23091` | `session_history` 12 条（user/assistant/tool 完整）；子代理中间读文件不泄漏进主干 |
| #3（结论型） | `co_84ede68fcb34` | `file_write`×2 → `spawn_agent`×2 → `wait_agent` → `subagent_end`×2 → `read_agent_result`×2 → DONE；两子代理 `completed, turns=2` |
| #4（S6 接入 hint + since_event_seq 之后复测） | `co_ac33eefb1109` | `file_write`×2 → `spawn_agent`×2 → `wait_agent` → `subagent_end`×2 → `read_agent_result`×2 → `close_agent`×2 → DONE；`subagent_runs`: scan_a/scan_b `completed, turns=2`；`session_history` 16 条（user 1 / assistant 6 / tool 9），子代理中间消息不泄漏进主干 |

E2E #1 暴露并修复的真实 bug：`build_turn_runtime` 派生 loop 时最初 `history_store=None`，
导致**主对话 assistant/tool 消息不落 `session_history`**；已改为 `history_store=base_runtime.store`，
并同步更新单测断言。修后 E2E #2 验证历史完整。

### 4.4 Playwright UI 验证

`work/_ui_subagent.py` / `_ui_shot3.py`：1440 / 390 两档 → 卡片 2 个、展开 1 个、
`h_overflow=0`、`console_errors=[]`。去重逻辑修复后截图 `work/subagent_card2.png` 显示正常。
S6 收尾复测（`co_ac33eefb1109`）：1440 / 390 两档均 `cards=2, h_overflow=0, console_errors=[]`；
截图 `work/subagent_p2_1440.png` / `work/subagent_p2_390.png`。

### 4.5 能力模型简化后的真实 LLM E2E

会话 `co_2417570bf2d8`：父代理 `file_write` → `spawn_agent(calc)` → `wait_agent` → `read_agent_result` → `close_agent`；子代理实际执行 `list_files → file_read → code_run`，`status=completed, turns=3`，`code_run` 脚本输出总和 172 / 均值 34.4 / 最大值 58，父代理汇总正确。证明子代理已继承 `code_run`（旧 explore 角色下会失败）。

## 5. 关键设计决策（为什么这么做）

| 决策 | 理由 |
|---|---|
| 进程内 asyncio，不起 OS 子进程 | 多用户下进程数爆炸；子代理瓶颈是 LLM 网络 IO |
| 子代理独立侧链 history（不写 session_history） | 否则主对话下一轮把子代理中间产物全读回来，上下文爆掉 |
| `fork_turns` 默认 `none`（GA 默认 all） | 多用户成本 + 上下文卫生 |
| 能力闭世界落在 ToolRegistry 层 | prompt 不是安全边界，registry 里没有才是 |
| `wait_agent` 超时 ≠ 失败 | 子代理继续跑，返回 `timed_out + remaining_targets`，避免重复 spawn |
| 槽位按 每 session/host，不是全局 | GA 全局槽位在多用户下会被一个用户占满 |
| 子代理写根隔离到 `.subagents/<session>/<name>` | 避免并发子代理写同一 workspace 互相覆盖 |
| v1 单进程假设（host 是内存状态） | 多实例演进路径见设计文档 §6.7 |

## 6. 已知限制（v1 有意不做）

1. **单进程**：`SubagentHost` 是内存注册表，多副本部署下重连到别的实例看不到子代理（§6.7 给了演进路径）。
2. `SubagentHost` 生命周期绑定「一次生成」：页面刷新/重连后旧卡片从 DB 侧链可读，但**不能在旧代上继续 wait**。
3. `background=false` 参数目前仍按后台任务执行（语义预留，不做前台阻塞式）。
4. `close_agent.reason` 仅透传占位，不入库。
5. `since_event_seq` 是**每节点内存环形缓冲**（最近 2000 条），重启即失效；持久化回放靠侧链表，不走游标。
6. 子代理默认继承父模型，尚不支持 `model_profile`（团队/成本优化阶段再加）。
7. 编排工具（`send_message` / `task_*` / `team_status`）已在 `ORCHESTRATION_TOOLS` 里预留常量，
   但v1 不注入（P3 team/task 阶段实现）。

## 7. 如何自测（照做即可）

```bash
# 1) 后端单测
cd backend && python -m pytest tests -q          # 期望 62 passed

# 2) 前端构建
cd frontend && npm run build                     # 期望 built successfully

# 3) 启动（热加载）
cd backend && python -m uvicorn app.main:app --reload --reload-dir app --port 8000
# 前端产物由后端静态托管，浏览器打开 http://127.0.0.1:8000/ 后 Ctrl+F5
```

浏览器里用 `test / 12345678` 登录，发送类似：

> 先在工作区写两个小文件 a.txt / b.txt。然后并行派两个子代理分别统计各文件行数，
> 一个叫 scan_a 一个叫 scan_b，等它们都完成后汇总告诉我。

预期：出现 2 张子代理卡片（可展开看 `file_read` / `code_run` 步骤），主回答给出汇总；
点「停止」时子代理一并停（`/api/sessions/{sid}/stop` 返回 `subagents_stopped`）。

## 8. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-05 | P2 实施完成：S1~S6 全部落地；64 单测 + build + 3 次真实 E2E + Playwright 验证通过 |

| 2026-11-05 | 能力模型简化：移除 explore/plan/review 只读角色；子代理统一继承父代理全部业务工具（含 code_run / Skill / MCP），唯一边界是不可调用编排工具。单测 62 passed |
