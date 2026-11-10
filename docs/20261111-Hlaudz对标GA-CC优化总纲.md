# Hlaudz 对标 GA / CC 的三向优化总纲

- 日期：2026-11-11
- 分支：`feat/ga-cc-alignment`
- 对标对象：
  - **GenericAgent**（`D:\git_codes\GenericAgent`，main @ `f234d92`，2026-11-10；近三天 69 个 commit）
  - **Claude Code v2.1.319 源码存档**（`D:\git_codes\claudecode283_hack`）
- 本文是**总纲**，只回答「做什么、先做哪个、怎么验收」。三份子方案给出逐条设计与测试：

| 方向 | 子方案 |
| --- | --- |
| ① 内置工具补齐（glob / grep / 预算 / 退出码 / 写前必读） | `20261111-Hlaudz-内置工具与结果预算方案.md` |
| ② subagent 异步与 agent team 稳健性 | `20261111-Hlaudz-subagent与team测试优化方案.md` |
| ③ 提示词与通用任务能力（非代码开发领域） | `20261111-Hlaudz-提示词与通用任务能力方案.md` |
| ④ 工具渐进式披露（ToolSearch / deferred tools）调研与决策 | `20261111-Hlaudz工具渐进式披露调研与决策.md` |

---

## 0. 一句话结论

Hlaudz 的**架构**不比 GA 差（同样是异步 subagent + 持久任务板 + 事件总线），
差在**工程成熟度**和**给模型的工具/提示词供给**：

1. **工具面**：只有 6 个内置工具，没有 `glob` / `grep`，`code_run` 是唯一搜索手段；
   所有工具共用 20,000 字符硬截断，没有落盘、没有 per-tool 上限、没有退出码语义。
2. **subagent / team**：功能齐全但**只有进程内单测**，没有真实 LLM 链路 e2e；
   GA 在 11-08～11-09 用真实 e2e 连续抓出 **12+ 个链路级缺陷**（通知丢失、僵尸 running、
   关闭竞态、任务悬挂），Hlaudz 很可能有同类问题，只是还没被测出来。
3. **提示词**：`SYSTEM_PROMPT` 约 1.3 KB、两节；GA 的 `sys_prompt.txt` 约 8 KB、九节，
   且经过 4 轮针对「专用工具优先 / skill 隐性触发 / 渐进式披露」的迭代。

目标不是把 Hlaudz 改成 GA，而是让 Hlaudz **在 Web 多用户场景下**获得同样的
「工具选择正确率」和「任务完成率」，并把 subagent/team 的可靠性从「单测绿」
提升到「真实链路绿」。

---

## 1. 现状事实（逐条带坐标）

> 全部在 `feat/ga-cc-alignment` 分支上核对；行号以写本文时为准。

### 1.1 工具面

`backend/app/builtin_tools.py:341 _build_tool_list()` 注册 6 个工具：

```
file_read, file_write, file_patch, list_files, code_run, load_skill
```

编排面 `backend/app/subagent/tools.py:19` 注册 5 个：
`spawn_agent, wait_agent, read_agent_result, list_agents, close_agent`。

Team 面 `backend/app/team_tools.py:34` 注册：`spawn_teammate, send_message,
list_messages, task_create, task_get, task_update, task_list, team_status`。

**没有 `file_glob`，没有 `file_grep`，没有独立 `bash`**（shell 能力混在 `code_run` 的
`code_type=bash/powershell` 里）。

### 1.2 工具结果处理

- `builtin_tools.py:34 MAX_OUTPUT_CHARS = 20000`，`_truncate()` 只保留头部 +
  一行脚注（`builtin_tools.py:43-48`）。
- 截断后**没有落盘路径**，模型拿不回来；超长结果直接丢尾部。
- `code_run` 输出 `[code_run python] exit=N cwd=...`（`builtin_tools.py:237`），
  `exit` 只是数字，模型要自己判断 `grep` 无匹配（exit 1）是不是错误。
- `file_read` 有 `start/count/keyword` 分页，但没有「读完整个文件」的记录，
  因此无法支撑「写前必读」。

### 1.3 并行与预算

- 未发现按工具分组的并行执行；工具调用由 `AgentLoop.run_stream`
  （`backend/app/agent_loop.py:161`）串行逐个执行。
- 没有统一的 `budget` / `limits` / `persisted-output` 模块。

### 1.4 subagent 与 team

架构上 Hlaudz 已经是异步的：

- `SubagentHost.spawn()`（`subagent/host.py:66`）`background=True` 时立即返回，
  子代理在独立 `asyncio.Task` 里跑（`host.py:139 _drive()`）。
- `wait_agent` 是真等待（`host.py:218`），超时不杀子代理（已有测试
  `test_wait_timeout_does_not_kill_child`）。
- 子代理事件与主代理事件合并到同一个 `EventBuffer`（`subagent/events.py`），
  由 SSE 单点消费。

**缺的**：

| 能力 | Hlaudz | GA（11-08 之后） | CC 319 |
| --- | --- | --- | --- |
| 向运行中的子代理追加消息（steer） | ❌ | ✅ `followup_task` | ✅ `SendMessage` |
| 子代理结果以「通知」注入母回合，而非只靠 `wait` | ❌（仅 `wait` + `read_agent_result`） | ✅ fold 点注入 + peek/ack | ✅ `task-notification` |
| `notify_when_idle` 订阅 | ❌ | ✅ | ✅ |
| 名字钉住防串名 | 仅有 `duplicate_name_rejected` | ✅ `name_pins.json` | ✅ `sendMessagePins` |
| 事件可重放（游标 + ack 分离） | 部分（`event_seq` 增量，内存） | ✅ 文件源 + 游标 | ✅ 进程内事件日志 |
| `wait_agent` 不阻塞回合 | ❌ 真阻塞 | ✅（4655f83） | ✅ |
| 真实 LLM 链路 e2e | ❌ 全是 mock | ✅ 6 轮真实回归 | ✅ transcript 实证 |

**这不是说 Hlaudz 的实现有明确 bug**，而是说：GA 用真实 e2e 抓出的那一类缺陷
（关闭竞态把干完活的成员记成 failed、已完成但仍在线的成员显示 running、任务挂在
已关闭成员上不归还），Hlaudz 的 36 个单测**测不到**，因为单测不走真实的
`spawn → 模型调用 → 工具执行 → 结束 → 回收` 全链路。

### 1.5 提示词

`backend/app/agent.py:25 SYSTEM_PROMPT` 只有两节：

- 工具使用与自纠（失败升级、多步核对、结构化结果不重复粘贴）
- 本地工作区与 Skill（workspace 边界、code_run 优先、产物路径红线、Markdown 图片）

对比 GA `assets/sys_prompt.txt`（77 行，九节）：身份、能力与边界、怎么工作、
规划与推进、代码与改动纪律、验证纪律、审查与产物质量、工具使用、沟通与交付。

Hlaudz 缺的主要是**非代码领域**仍然适用的四条：规划与推进、验证纪律、
产物质量、沟通与交付。以及 GA 第 4 轮迭代出的两条关键工具策略：
**专用工具优先**、**skill 按任务类型隐性触发**。

---

## 2. 差距矩阵（按性价比排序）

| # | 差距 | 影响 | 工作量 | 优先级 |
| --- | --- | --- | --- | --- |
| 1 | 没有 `file_glob` / `file_grep` | 模型被迫用 `code_run` 写 `find/grep`，慢且不可靠、输出超限 | 小 | **P0** |
| 2 | 20K 硬截断、不落盘 | 长输出（日志/测试/大文件）直接丢尾部，任务卡死 | 小 | **P0** |
| 3 | 退出码无语义 | `grep` 无匹配 / `diff` 有差异被当成错误，模型误判 | 小 | **P0** |
| 4 | subagent/team 无真实链路 e2e | 12+ 类链路缺陷测不出来 | 中 | **P0** |
| 5 | 提示词缺「规划/验证/交付」与「专用工具优先」 | 任务完成率、重复劳动、假完成 | 小 | **P1** |
| 6 | `wait_agent` 阻塞回合 | 派活后母代理空转等待，无法并行做别的 | 中 | **P1** |
| 7 | 无 steer（向运行中子代理追加消息） | 派错任务只能关掉重派，浪费已完成工作 | 中 | **P2** |
| 8 | 写前必读未强制 | 覆盖别人的/自己的改动，产物被静默冲掉 | 小 | **P2** |
| 9 | 无通知 fold 注入 | 子代理完成必须靠轮询 `wait`，回合结束就丢 | 大 | **P2** |
| 10 | 提示词 skill 隐性触发 | 该用的 skill 不用，重复踩坑 | 小 | **P1** |

---

## 3. 分阶段路线图

每个阶段独立可交付、独立可回滚；每阶段结束跑全量 `pytest` + 该阶段新增 e2e。
**阶段内严格 TDD**（见 §4），不允许先写实现后补测试。

### P0 — 让模型有正确的工具可用（预计 2 个工作日）

| 步 | 内容 | 交付 |
| --- | --- | --- |
| P0-1 | `backend/app/file_search.py`：`file_glob` / `file_grep`（优先 `rg`，回退 Python），注册进 `_build_tool_list` | 2 个新工具，schema 中文描述 |
| P0-2 | `backend/app/tool_budget.py`：per-tool 上限（glob 100K / grep 20K / code_run 30K / file_read 默认 50K）+ 超限落盘 `<persisted-output>` | 统一预算层，环境变量可调 |
| P0-3 | `backend/app/exit_codes.py`：`classify_exit_code()`，`grep`/`diff`/`test`/`find` 非零归 `informational` | `code_run` 返回带语义 |
| P0-4 | 工具结果同时进 SSE（前端可展开完整输出） | UI 不回归 |
| P0-5 | 真实 LLM e2e：让模型「在仓库里找所有 TODO 并统计」——必须用 `file_grep`，不许用 `code_run` | e2e 断言工具名 |

### P0.5 — subagent / team 真实链路体检（预计 2 个工作日）

| 步 | 内容 |
| --- | --- |
| A | 写一套**真实 LLM** 的 e2e 骨架（复用 GA 的思路：runId + 事件落盘 + 事后审计） |
| B | 跑 6 个基础场景，记录真实缺陷（预期能复现 3-8 个） |
| C | 每个缺陷按 TDD 修：先写能复现的单测/集成测试，再修 |
| D | 规模场景：8 个简单任务 + 3 个中等任务的 team 回归 |

### P1 — 提示词与回合效率（预计 2 个工作日）

| 步 | 内容 |
| --- | --- |
| P1-1 | 重写 `SYSTEM_PROMPT`：补「规划与推进 / 验证纪律 / 审查与产物质量 / 沟通与交付」 |
| P1-2 | 加「专用工具优先」段：`file_glob` / `file_grep` / `file_read` 优先，`code_run` 只做 shell-only |
| P1-3 | skill 按**任务类型**隐性触发（对标 GA 第 2 轮） |
| P1-4 | skill 索引预算与描述完整性（对标 GA 第 3/4 轮：索引 16K、描述 1536） |
| P1-5 | `wait_agent` 改为「非阻塞回执 + 结果可后续读取」（对标 GA 4655f83） |

### P2 — 协作能力补全（预计 3 个工作日）

| 步 | 内容 |
| --- | --- |
| P2-1 | `followup_task`（向运行中子代理追加消息，steer） |
| P2-2 | 名字钉住（防重名/串名） |
| P2-3 | 子代理完成通知在 fold 点注入母对话（对标 GA 2.2 / CC `task-notification`） |
| P2-4 | `notify_when_idle` 订阅 |
| P2-5 | 写前必读门禁（对标 GA 2.4） |

### P3 — 打磨（可选）

- 事件持久化换 SQLite/文件源（对齐 GA 的 event log）
- team 面板「未知状态」观测
- 并发工具执行分组（只读并行、写串行）

---

## 4. 测试总策略

### 4.1 三层测试，缺一不可

| 层 | 形态 | 抓什么 | 现有 |
| --- | --- | --- | --- |
| L1 单测 | pytest + 假对象 | 分支、边界、CAS、权限 | ✅ 36 个 |
| L2 集成 | 假 LLM（脚本化 tool_calls） | 链路编排、事件顺序、回收 | 部分 |
| L3 真实 e2e | 真 LLM + 真工具 + 真前端/SSE | 链路级缺陷、模型行为 | ❌ **缺** |

**GA 的经验**：L1 全绿的情况下，L3 仍然抓出 12+ 个真缺陷。所以 P0.5 必须先建 L3。

### 4.2 铁律（沿用本仓库 `AGENTS.md` §8.5）

1. **RED 先行**：没有先失败的测试，不写生产代码。
2. **反向确认**：测试变绿后 `git stash` 掉实现，测试必须重新变红。
3. **硬信号**：判断「通知送达 / 消息入队」不能用「没有错误」当证据，
   载荷要带本次唯一的随机 token，要求下游复述。
4. **静态守卫用 AST**，不用纯文本匹配。

### 4.3 L3 e2e 的判定方式

对齐 GA 与 CC 的做法：

- 每次运行生成 `runId`，全部 SSE 事件落盘到 `temp/e2e/<runId>/events.jsonl`；
- 事后**读事件文件**审计，不读 stdout 字符流（GA 踩过的坑）；
- 断言分三级：`构造得出` < `运行时发了` < `用户看到了`；
- 负对照：同一提示词换一个不该触发该工具的任务，确认工具没被调用。

---

## 5. 明确不做

- **不引入 GA 的 `task_create/get/update/list` 之外的任务板语义**——Hlaudz 已有 team 任务板。
- **不做 workflow 脚本引擎**——用户本次没要求，且与 Web 多租户沙箱风险叠加。
- **不照搬 GA 的 `codex_lesson_update` / `start_long_term_update`**——依赖 GA 自身机制。
- **不在 GA 仓库做任何改动**——GA 仍在开发，本轮只读。
- **不改现有 A2A / ACP / 图片上传**——与本轮无交集，避免牵连。
- **不在固定工具面（19 个 / 8.7 KB）上做 tool_search**——受益为负；但 MCP 膨胀时的阈值与切换方案已写死，见子方案 ④。

---

## 6. 验收定义（Definition of Done）

一条优化只有同时满足以下四条才算完成：

1. 全部新增测试 + 既有全量测试通过（`pytest backend/tests -q`）；
2. 至少一条 L3 真实 e2e 覆盖该行为，且带负对照；
3. 反向确认做过（stash 实现 → 测试变红 → 恢复）；
4. 文档更新：本文或子方案标注「已实施 + 实测结果 + 遗留问题」。

---

## 7. 实施进度（2026-11-11 回填）

| 阶段 | 状态 | 记录 |
| --- | --- | --- |
| P0 内置工具与预算 | ✅ 已完成 | `20261111-Hlaudz-P0实施与e2e实测记录.md` |
| P0.5 e2e 骨架 | ✅ 已完成（S0/S1/S2/S3/S4/S5/S6/S7/S8/S9/S10/S12） | 同上 §3 + 压测报告 |
| P1 提示词与 skill 索引 | ✅ 已完成；消融已跑（结论：无回归，未证明提升） | `20261111-Hlaudz-提示词消融与UX五项实施记录.md` |
| P2 协作能力补全 | ✅ 已完成（steer / 名字钉住 / 通知注入 / 跨回合持久化） | `20261111-Hlaudz-P2实施记录.md` |
| 子代理 UX 五项 | ✅ 已完成（去重 / 不静默截断 / stopping / 耗时 / 失败展开） | 同上 §3 |
| P3 打磨 | 🟡 部分：只读并发已完成；事件持久化、team 面板未开始 | 压测报告 §5.2 |

单测 **414 passed**、前端 16 passed；真实 e2e 已跑 12 个场景
（S0–S10 + S13 + 跨回合 S12），底座为湛卢模型。

### 7.1 本轮压测抓到的真实缺陷

| # | 缺陷 | 发现方式 |
| --- | --- | --- |
| 1 | teammate 事件进不了 lead 的 SSE（面板正确率 30%） | S6 规模 + audit 的 SSE/DB 对比 |
| 2 | 子代理墙钟默认 300s，长任务被静默杀掉 | S10 12 分钟长任务 |
| 3 | 跨回合 read_agent_result 读不到上一代结果（+ KeyError） | cross_turn 两轮对话 |
| 4 | `subagent_status` 没写进 `chat.py` 的事件白名单，会被静默丢弃 | 实现 stopping 时发现，S13 实证 |

## 8. 未完成清单（截至 2026-11-11）

### 8.1 方案里明确要做、但还没做的

| # | 项 | 出处 | 影响 |
| --- | --- | --- | --- |
| 1 | skill 索引在真实 20+ skill 账号下的实测 | 提示词方案 §5.1 | 16K/1536 是否够用未验证 |
| 2 | S11 大输出落盘场景：场景已加进 `run.py`，**尚未跑** | 子方案 ①§8.6 | 仅单测覆盖，没有真实模型验证 |
| 3 | S1–S8 连跑 3 次确认无 flake | 子方案 ②§9 | 稳定性只有一次性证据 |
| 4 | 针对「不中途放弃 / 完成必须有证据」的用例（失败重试、依赖缺失类） | 消融 §5.3 | 新提示词的核心主张仍未被量化 |

### 8.2 P3 打磨（总纲 §3 明确列为可选）

| # | 项 | 现状 |
| --- | --- | --- |
| 6 | 事件 `seq` 跨重启持久化 | 内存游标，重启归零 |
| 7 | team 面板「未知状态」观测 | 未做 |
| 8 | MCP 工具膨胀时的 `tool_search` 阈值切换 | 方案已写死（>20KB 或 >24 个触发），未实现也未触发过 |

### 8.3 压测覆盖但结论待补的边界

| # | 项 | 状态 |
| --- | --- | --- |
| 9 | >1 小时的单任务 | 只验证到 12.7 分钟 |
| 10 | 跨回合通知在「关窗重开」下的去重 | 未验证 |
| 11 | teammate 在长任务中被关闭时的任务归还 | 未验证（GA 的 D4 类风险） |
| 12 | `wait` 与通知的毫秒级时序 | 已知，判断为不影响，未修 |

### 8.4 明确不做（不是遗漏）

- 常驻 teammate + `notify_when_idle`：与「一次性子代理」模型冲突，等设计变更；
- workflow 脚本引擎：用户本轮未要求；
- GA 的 `codex_lesson_update` / `start_long_term_update`：依赖 GA 自身机制；
- 固定工具面（19 个）做 tool_search：实测 8.7 KB，收益为负。
