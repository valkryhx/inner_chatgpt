# AGENTS.md — 本项目实施指南（后续 agent / 协作者从这里开始）

> 这份文件是**项目级交接文档**。目标：任何一个新 agent 只读本文件 + 它指向的几份文档，
> 就能在不问人的情况下继续推进。**开工前先读完本文件。**

---

## 0. 项目定位与铁律（不可违背）

**本项目 = 以 GenericAgent 为内核的 Web 多用户 Agent 服务。**

- 来源：当前代码是 `D:\git_codes\jiakuan_langchain` 的初始快照（曾被强制要求基于
  LangChain + LangGraph），现已决策**去除这两个库**，改为借鉴 `D:\git_codes\GenericAgent`
  的设计、在本项目重写自研内核。
- 形态：Web 服务（FastAPI + React），多用户、会话隔离、SQLite 持久化、MCP 工具接入、
  SSE 流式、上下文压缩。
- 目标：逐步把 GenericAgent 的成熟能力（代码执行 / 文件编辑 / subagent / team / task /
  计划模式 / goal / hooks）搬进 Web 多用户形态。浏览器控制暂不做。

铁律（不可违背）：

1. **只写真实可运行代码。** 不写伪代码、不写“示意性实现”。
2. **不引入 langchain / langgraph。** 新代码一律自研；需要参考时看 GenericAgent，不 import 它。
3. **借鉴设计，不 import GA 包。** GenericAgent 的 `handler.py`（约 13.6 万字符）、
   `app.py`（约 14.6 万字符）是 CLI 总装层，**不得整体搬入**；只借其循环/模型/压缩/子代理设计。
4. **契约稳定。** SSE 事件名与字段、HTTP API 路径与响应结构、前端契约不得破坏。
5. **每阶段可独立验证。** 允许新旧实现并存、用开关切换，验证通过再删旧代码。
6. **不丢历史数据。** 任何迁移前先备份 `backend/data/app.db`（含 `-wal` / `-shm`）。
7. **写文件一律 UTF-8 / LF**（无 BOM）。
8. **不覆盖前一阶段产物**：每阶段输出到自己的目录/文档。

### 0.1 当前阶段

**P0：去 LangChain / LangGraph 的轻量化基线替换 —— 已完成（2026-11-05）。**

- 后端已无任何 `langchain* / langgraph*` import；`requirements.txt` 已移除 5 个相关依赖，新增 `httpx`。
- 自研内核 4 件套：`llm_client.py`（httpx，chat/responses 双线流式）、`tools.py`（Tool/ToolRegistry）、
  `history_store.py`（`session_history` 单表历史）、`agent_loop.py`（显式工具循环 + 悬空 tool_call 自愈）。
- 历史数据已迁移：89 个会话从 langgraph checkpoint 解码写入 `session_history`（迁移前已备份 DB）。
- 真实 LLM E2E 通过；`backend/tests` 单测通过。

**P1：Skill 模块 + 基础工具（read / write / patch / runcode / load_skill） —— 已完成（2026-11-05）。**

已完成内容（详见 `docs/20261105-changelog-SkillP1.md`）：

- 内置工具（注册进同一 `ToolRegistry`，与 MCP 工具同构）：
  `file_read` / `file_write` / `file_patch` / `list_files` / `code_run` / `load_skill`。
- Skill 模块：文件系统 `SKILL.md`，双轨存储（`backend/skills_builtin/` 只读内置 +
  `backend/data/skills/<user_id>/` 私有）；**渐进披露**（system prompt 只放 L1 清单，
  正文由 `load_skill` 按需加载 L2）。
- 用户隔离：所有文件工具限制在 `backend/data/workspaces/<user_id>/`；路径 `resolve()` 后校验，拒绝越界。
- API：`/api/me/skills` CRUD + `/api/skills` 内置列表；前端「设置 → 我的 Skills」tab。
- 单测 23 项通过（原 12 + 新增 11），前端 `npm run build` 通过，真实 LLM E2E 验证
  `load_skill → file_write → code_run` 全链路。

**P2：Subagent（进程内 asyncio 子代理运行时）—— 已完成（2026-11-05）。**

已完成内容（详见 `docs/20261105-changelog-SubagentP2.md`）：

- 新包 `backend/app/subagent/`：`context`（AgentContext 多租户身份贯穿）/`store`（subagent_runs +
  subagent_messages 侧链表）/`events`（StopSignal + EventBuffer 单队列）/
  `capabilities`（闭世界工具裁剪）/`registry`（深度/槽位/重名/淘汰护栏）/
  `runtime`（子代理 Runtime + 写根隔离）/`host`（spawn/wait/read/list/close 生命周期）/
  `tools`（5 个编排工具）/`prompts`（root + child 提示词）/`session`（每轮派生 TurnRuntime）。
- 5 个编排工具只注入 root agent；子代理 registry 里**根本不存在它们**（prompt 不是安全边界）。
  子代理统一继承父代理全部业务工具（file/code_run/Skill/MCP），不再设 explore/plan/review 只读角色。
- 子代理用**独立侧链 history**，不写 `session_history`，避免主对话被中间产物污染。
- 子代理事件并入主对话同一条 SSE（`subagent_start` / `subagent_event` / `subagent_end`），
  前端新增可折叠 `SubagentCard`。
- 停止级联（主 stop → 所有子代理 stop + cancel）、`wait` 超时不杀子、墙钟 `SUBAGENT_TIMEOUT_S`（2026-11-11 起默认 1800s，可按次用 `spawn_agent(timeout_seconds=…)` 覆盖）、
  `wait_agent.since_event_seq` 增量事件。
- 单测 62 项通过（原 42 + 新增 20），前端 `npm run build` 通过，3 次真实 LLM E2E + Playwright UI 验证。

**P3：Agent Team-lite（Web 多用户隔离基线）—— 已完成（2026-11-06）。**

已完成内容（详见 `docs/20261106-changelog-AgentTeamP3.md`）：

- 四级作用域 `user_id -> session_id -> generation_id -> team_id` 贯穿 Team/任务/消息/事件/工具。
- 持久任务板、`blocked_by` 依赖、owner 软锁、CAS `expected_version`、失败 spawn 回收。
- Root 为 lead；teammate 继承 file/code_run/Skill/MCP，但不能调用编排工具或递归 spawn。
- active generation 唯一租约、团队生命周期租约、用户/团队资源闸门、停止级联。
- 前端右侧 TeamPanel（任务 / 消息 / 动态），SSE 增量更新、刷新 REST 恢复。
- 停止链路加固：`/stop` 同时 set event + cancel task + 级联子代理/code_run；发送/读路径自动收敛无 live task 的陈旧租约；前端本地优先停止。详见 `docs/20261106-Agent停止卡死与陈旧租约自愈.md`。
- Windows `--reload` 下 `code_run` 的 SelectorEventLoop 子进程兼容层。

**P3 后续补强 —— 已完成（2026-11-06）：**

- **任意格式文件下载**：`/api/me/workspace-file`、`/workspace-files`、`/workspace-zip` 三端点；
  前端 `FileChip` / `WorkspacePanel` / 工具步骤下载三入口；Office 生成依赖内置；
  154 项单测 + 16 项真实 E2E。详见 `docs/20261106-任意格式文件下载实施记录.md`。
- **Web 自测 14 项 Bug 修复**：子代理角色、Team 配额泄漏、压缩不变量、前端可见性等逐条修复；
  详见 `docs/20261106-Web自测Bug修复记录.md`。
- **停止/续任务加固**：协作旗 + `task.cancel` + 子代理/`code_run` 级联；陈旧 generation 租约自愈；
  停止后继续发送、图片滚动、首帧可见等前端修复。
- **文档刷新（2026-11-09）**：按 `main@3b9c68f` 重写 `README.md`，补全 `docs/20261106-项目功能实现原理.md`
  的 Team / 文件下载 / 配额 / 进程守卫章节。详见 `docs/20261109-项目现状与文档刷新.md`。

**P4（下一步，未开始）：** 计划模式 → goal → hooks。浏览器控制暂不做。
路线图见 `docs/20261105-去LangChain依赖-决策记录.md`。
开工前先按 0.3 的文档约定新建设计文档，再动代码；不得把未实现项写进 README / 本文件的能力描述。

**P5：外部 GenericAgent（ACP）—— 已实现，manager 级真 GA E2E 通过（预检缺陷已修，2026-11-09）。**
（P5 与 P4 并行推进、先于 P4 落地；**P4 仍未开始**，下面的 P4 条目保持原样。）

- 新增 `backend/app/acp/`：自写 JSON-RPC 行传输（`transport`）、握手与请求分发（`client`）、会话状态机与事件映射（`session`）、并发配额/会话名额/进程租约/停止级联（`manager`）、5 个 root-only 工具（`tools`）与只解析环境变量的配置层（`config`）。
- 与既有 P2/P3 同构：GA 事件并入主对话 SSE（`subagent_start/_event/_end`），GA 进程注册 `ProcessLease`，`/stop` 走「优雅 cancel → 硬杀兜底」。
- 真 GA manager 级 E2E 通过（HEAD `b106234`，全新进程无缓存预热），三个场景：① 哨兵 7.9 s，`file_write` 落盘 19 B（哨兵 + GA 自带行尾换行，含哨兵即判通过）；② 多步 15.8 s，`file_read → file_patch → code_run` 共 3 个工具步骤，`calc.py` 被改对、独立复核 `python calc.py` rc=0 / `ALL TESTS PASSED`；③ 长任务停止分层回收（`process_registry` 硬杀后 `returncode=1`），且 `ga_stop` 后 2.7 s 内能再派新任务并复述随机 marker。
- **预检缺陷（E2E 中发现，已修复）**：原探针 `print(b.__file__)` 被真 bridge 在 import 期对 fd 1 / text stdout 的重定向击穿（真机预检恒失败 → `ga_*` 不注册；单测用假模块所以全绿）。`78c0b4f` 改为 `importlib.util.find_spec(<module>).origin`（只解析路径、不执行目标模块），并补了带 stdout 重定向副作用的回归测试；复验：全新进程预检 0.047 s 返回 site-packages 路径 + SHA256 `4e95c64b…`，5 个 `ga_*` 正常注入。详见记录 §6。
- 未覆盖：Web UI E2E；DB `blocks` / 「GA 外援」卡片渲染（需完整 turn runtime + UI）；chat / SSE 层「停止后立即发送」（manager 层已证可继续）；GA 内部 subagent 事件透出；跨轮续跑（bridge `loadSession: false`）。另：`tasklist | findstr genericagent_acp_bridge` 对 GA 是假阴性，复核请按命令行过滤（见记录 §5.5）。
- 详见 `docs/20261109-ACP-client驱动GenericAgent实施记录.md`；设计与工具契约见 `docs/20261109-ACP-client驱动GenericAgent设计与实施.md`。

**P6：A2A 接入（对外调用面）—— Phase 1/2 + 生产硬化基础已完成（2026-11-10）。**

- 新增 `backend/app/a2a/`：每用户身份与卡片（`store` / `cards`）、哈希 Bearer token 与三重绑定（`tokens` / `auth`）、JSON-RPC 与 SSE（`router` / `service` / `stream`）、进程内事件日志（`events`）、配置（`config`）。默认关闭，`INNER_A2A_ENABLED=true` 才暴露。
- **权限模型是静态的、按 token 的**：`backend/app/tool_policy.py` 定义 `full` / `read_only` / `chat_only` 三档，建 token 时定死，`build_turn_runtime(..., tool_policy=)` 在装配 registry 时过滤。**不做逐次人工审批**——Hlaudz 默认 full access。
- 并发配额（`INNER_A2A_MAX_ACTIVE_TASKS_PER_USER` / `..._GLOBAL`，超限 429 + `-32010`）与审计表 `a2a_audit`（最近 2 万行）已生效。
- 终态任务可 `SubscribeToTask`：发 `task` → 最终 artifact → 终态 status 后关流；重启被标 `FAILED` 的任务同样可见原因。
- 验证：`backend/tests/test_a2a_mvp.py`（13）+ `backend/tests/test_a2a_policy.py`（12），后端全量 271 passed；真实 A2A CLI v0.4.0 E2E（同步/流式/订阅/取消/幂等/配额/档位）。
- 详见 `docs/20261110-A2A使用文档.md`、`docs/20261110-A2A-MVP实施记录.md`；协议调研见 `docs/20261109-A2A协议接入调研与设计.md`。

**P7：图片上传与视觉输入 —— 已完成（2026-11-10）。**

- 存储：`backend/data/workspaces/<user_id>/uploads/<uuid>.<ext>`；DB 只存引用（`messages.blocks` 的
  `attachments` 块），历史里不存 base64。
- 链路：`POST /api/me/uploads` → `SendMessageIn.attachments` → `sse.run_agent_stream` →
  `agent_loop` 送模型前用**副本**展开成 data URL → `llm_client` 转 `image_url` / `input_image`。
- 真实 E2E：带图问「图里是什么数字」答 `42`；新会话无图时明确回答"没有图片附件"（负对照）。
- 限制：单条 4 张 / 单张 25MB；只带最近 3 轮图片；删消息不删文件（无 GC）；A2A 暂不传图。
- 详见 `docs/20261110-图片上传与视觉输入设计方案.md`、`docs/20261110-changelog-图片上传与视觉输入.md`。

**P8：对标 GA / CC 的工具与提示词优化（P0 阶段）—— 已完成（2026-11-11）。**

- 内置工具：新增 `file_glob` / `file_grep`；统一结果预算（per-tool 上限 + 超限落盘 + 头尾预览）；
  退出码语义化（grep/diff/test/find 非零不再当错误）；写前必读门禁；只读工具并发。
- 提示词：`SYSTEM_PROMPT` 从 2 节扩到 7 节（补规划 / 验证纪律 / 工具优先 / skill 隐性触发）；
  skill 索引预算 8K→16K、单条描述 250→1536。
- 验证：新增 81 个单测（全量 373 passed）；真实 LLM e2e 骨架 `backend/tests/e2e/` +
  `.claude/skills/hlaudz-e2e/`，6 个场景（S0/S1/S2/S5/S7/S8）全绿。
- 详见 `docs/20261111-Hlaudz-P0实施与e2e实测记录.md`。

**MCP 链路加固 —— 已完成（2026-11-10）。**

- 被测提交 `b44ea3f`（工具来源可见 / 连接状态可见 / 软 TTL）三个目标实测通过：
  真实湛卢对话能说出 sqlbot 的 12 个工具、3/3 次取数结果与库内值一致、断链如实报告、
  前端侧栏与设置页红绿状态正确。
- 同时抓出并修复 3 个单测测不到的链路缺陷：
  ① **会话僵死不自愈（高）**：MCP server 重启后缓存 client 报 `Session terminated`，
  而 TTL 探测用新连接看到「没变」不重建 → 用户永久卡死，只能重启后端。
  修复：`MCPToolset.reconnect()` + 调用级自动重连重试 + `runtime._toolset_is_live()` 探活；
  ② **跨 server 同名工具冲突**：schema 出现重名 function，dispatch 静默调错 server。
  修复：`merge()` 冲突加 server 前缀并在提示词披露；
  ③ **MCP 结果无预算**：`run_readonly_sql` 一次 115 KB 原样进 history。
  修复：MCP 结果走统一预算层（实测 115,685 → 4,160 字符 + 落盘路径）。
- 另修 `/api/mcp/tools` 丢来源前缀的契约不一致。
- 验证：单测 451 → **488 passed**；7 套真实验证（后端 SSE + Playwright +
  临时 MCP server 重启复现），全部 0 失败，含反向确认。
- 详见 `docs/20261110-MCP与Hlaudz全链路测试与缺陷修复.md`。

### 0.2 必读文档（按顺序）

| 文档 | 用途 |
|---|---|
| `docs/20261113-CC源码结论核验方法论.md` | **源码调研核验方法论（长期有效）**：推翻旧结论的旁证要求、import 图为何不是证据（共享 chunk 被 1263 个模块引用）、断言「进上下文」须走到终态、措辞纪律与返工检查清单。从记忆机制调研的二次核验中提炼 |
| `docs/20261112-Hlaudz首帧串行阻塞核实与优化设计.md` | **首帧串行阻塞（已实测）**：软线压缩在首帧前调 LLM（超软线首帧 719→5859ms）、注入点无超时、无慢件延期通道；P1–P5 改造与 300ms 硬指标 |
| `docs/20261113-CC319记忆机制源码核实v2.md` | **CC 记忆机制权威调研（最新，取代 20261112 版）**：6 条读路径（含 `relevant_memories` 相关性选择器、`AutoMem` 索引注入、MCP 记忆库、账号记忆）/ 2 条写路径（`extractMemories` + `auto_dream` 合并）/ 四类记忆提示词全文 / 隔离与 28 个环境变量分类，逐条带行号证据 |
| `docs/20261112-Hlaudz记忆系统实施方案.md` | **记忆系统实施方案（设计，未开工）**：读侧复用 `message_injector` attachment 通道、写侧复用 subagent 后台运行时、`memories/<user_id>/` 物理隔离 + DB 行级隔离；M1–M5 分阶段与 S-M1~S-M5 e2e 用例。**开工前须按 v2 调研 §14.7 修正其 §2 类型模型 / §4.2 闸门 / §5 工具面** |
| `docs/20261112-CC319记忆系统源码调研.md` | **已被 `20261113-…核实v2.md` 取代**：方向（attachment 而非 hook）成立，但 `MEMORY.md` 注入方式与 `agent-memory*` 作用域含义有误，且漏掉 `relevant_memories` / MCP 记忆库 / `auto_dream` 三套机制。仅供追溯 |
| `docs/20261111-Hlaudz对标GA-CC优化总纲.md` | **本轮优化总纲（最新）**：对标 GA main@f234d92 与 CC 319 的现状坐标、10 项差距矩阵、P0–P3 路线图、三层测试策略 |
| `docs/20261111-Hlaudz-内置工具与结果预算方案.md` | 方向 ①：`file_glob`/`file_grep`、统一预算层、退出码语义、写前必读、并行分组 |
| `docs/20261111-Hlaudz-subagent与team测试优化方案.md` | 方向 ②：L3 真实 e2e 骨架 S1–S8、GA 12 类缺陷目录、steer / 名字钉住 / 通知注入 / 关闭竞态 |
| `docs/20261111-Hlaudz-提示词与通用任务能力方案.md` | 方向 ③：SYSTEM_PROMPT 2→7 节、专用工具优先、skill 隐性触发、消融测试 |
| `.claude/skills/hlaudz-e2e/SKILL.md` | **真实 e2e 验证 skill（强制使用，见 §2）** |
| `docs/20261111-Hlaudz-P0实施与e2e实测记录.md` | **P0 实施与 e2e 实测（最新）**：改动清单、81 条新测、反向确认与变异验证、6 个 runId 与审计结论 |
| `docs/20261111-Hlaudz-P2实施记录.md` | **P2 实施（最新）**：followup_task steer、名字钉住、通知 fold 注入、S4 实测与两个审计发现 |
| `docs/20261111-Hlaudz-e2e压测报告.md` | **e2e 压测（最新，含真实缺陷）**：S3 超时 / S6 13 任务规模；抓到 teammate 事件进不了 SSE 的缺陷（面板正确率 30%），修复后 98% |
| `docs/20261111-Hlaudz工具渐进式披露调研与决策.md` | 方向 ④：ToolSearch / deferred tools 调研（实测 8.7 KB）、MCP 膨胀阈值与切换方案、skill 索引差距 |
| `docs/20261110-MCP与Hlaudz全链路测试与缺陷修复.md` | **MCP 全链路测试（最新）**：`b44ea3f` 三目标实测通过；抓出并修复 3 个链路缺陷（会话僵死不自愈 / 跨 server 同名工具冲突 / MCP 结果无预算）+ 1 处契约不一致；单测 451→488，7 套真实验证 runId |
| `docs/20261110-MCP工具来源可见性与热生效验证.md` | MCP 工具来源 `[MCP: <server>]` 前缀、server manifest 进提示词、状态可见与软 TTL（本次测试的被测对象） |
| `docs/20261110-A2A使用文档.md` | **A2A 使用文档（含本机实跑任务 id）**：每用户 endpoint/token、能力档位、配额、审计、流式与终态订阅 |
| `docs/20261110-A2A-MVP实施记录.md` | **A2A 实施记录**：方法集、数据模型、安全模型、测试与真实 E2E |
| `docs/20261109-项目现状与文档刷新.md` | **文档刷新记录（最新）**：按 `main@3b9c68f` 核对的能力快照、README/AGENTS/实现原理的修改清单、未纳入项与后续建议 |
| `docs/20261106-AgentTeam轮次与跨轮任务恢复修复.md` | **Team 轮次/恢复修复**：subagent 默认轮数 300、`max_turns` 全链路透传、跨 generation 任务板快照恢复、主轮结束后等待 teammate、真实测试清单 |
| `docs/20261106-AgentTeam侧栏展开状态稳定修复.md` | **Team 侧栏展开状态稳定修复**：SSE `team_event` 协议错位、展开壳与数据解耦、展开偏好持久化、真实浏览器 E2E |
| `docs/20261106-Agent停止卡死与陈旧租约自愈.md` | **停止可靠性（最新）**：协作停 + task.cancel + 资源级联三层停止、进程内 generation registry、reservation TTL、陈旧 active 租约自愈、前端本地优先停止、真实 LLM E2E |
| `docs/20261106-changelog-AgentTeamP3.md` | **P3 已完成清单 + 真实 E2E 验收（最新，先读这份）** |
| `docs/20261106-AgentTeam-Web多用户隔离设计方案.md` | **P3 Team 设计：四级作用域、任务所有权、资源闸门、停止语义** |
| `docs/20261105-changelog-SubagentP2.md` | P2 已完成清单 + 自测 |
| `docs/20261105-Markdown图片渲染修复.md` | **Markdown / 原生 `<img>` 图片修复**：workspace 鉴权接口、Windows 路径、HTML 清洗白名单、`code_run` 产物边界 |
| `docs/20261105-changelog-停止生成内容保留.md` | **停止生成内容保留**：SSE 增量快照、stop 返回消息快照、前端停止态合并与空态判断 |
| `docs/20261105-changelog-SkillP1.md` | **P1 已完成清单 + Skill/基础工具自测（先读这份）** |
| `docs/20261105-Skill与基础工具设计与实施.md` | P1 设计：存储/隔离/渐进披露/工具边界/测试 |
| `docs/20261105-changelog-P0.md` | P0 已完成清单 + 如何自测 |
| `docs/20261105-去LangChain依赖-决策记录.md` | 决策、边界、路线图 |
| `docs/20261105-去LangChain依赖-实施文档.md` | P0 可执行蓝本（模块设计/步骤/风险/测试） |
| `docs/20261105-LangChain-LangGraph依赖审计.md` | 旧代码审计（用在哪、如何替换） |
| `docs/20261105-前端审美提升设计.md` | **前端审美提升方案（Design System v2）**：现状诊断、暖纸+陶土配色 token、去气泡消息流、配色对比度实测、分批实施与验收清单 |
| `docs/20261105-changelog-前端批次1.md` | **前端批次 1 已完成**：Token 层替换（暖纸+陶土）、四档位回归通过、两个响应式缺陷修复 |
| `docs/20261105-changelog-前端批次2.md` | **前端批次 2 已完成**：文本去气泡（assistant 零容器 / user 保留输入容器）、工具改步骤行 StepGroup、思考默认折叠、`--measure` 宽度自适应 |
| `docs/20261105-changelog-侧栏身份区与设置弹窗.md` | **侧栏身份区重做 + 设置弹窗定高**：全名替字母头像、整行可点进设置、登出降级为次要动作；modal 固定高度，切 tab 不跳尺寸 |
| `docs/20261105-Skill目录包导入与公用技能设计与实施.md` | **Skill 目录包导入 + 公用技能层**：三层模型 builtin/shared/user、zip 安全解压（穿越/软链/炸弹防护）、`backend/skills_shared/` 公用目录、已导入 2 个 Codex 技能 |
| `docs/20261105-Subagent调研与设计方案.md` | **P2 设计文档（已实施）**：GA 进程树 vs Aivory 子任务 的调研对比、进程内 asyncio subagent 架构、`AgentContext` 多租户贯穿、5 个编排工具契约、停止/超时/失败语义、前端 subagent block、6 阶段实施蓝本与测试计划 |
| `docs/20261106-Web自测Bug修复记录.md` | **Web 自测 14 项 Bug 修复记录**：子代理角色/Team 配额泄漏/压缩不变量/前端可见性逐条根因、改动文件、真实环境复验命令 |
| `docs/20261106-Web自测Bug清单.md` | **Web 自测 Bug 清单（原始）**：14 项问题的复现步骤、现象与影响面，配合修复记录阅读 |
| `docs/20261106-停止后继续发送白屏修复.md` | 停止后继续发送白屏的根因与修复 |
| `docs/20261106-图片消息滚动跟随修复.md` | 图片渲染后自动滚动跟随修复 |
| `docs/20261106-主Agent轮数上限提升至300.md` | 主 Agent 轮数上限从默认值提升到 300 的说明 |
| `docs/20261106-发送首帧可见与执行尾注.md` | 发送后首帧可见与执行尾注展示 |
| `docs/20261106-Web自测Bug修复-测试方案.md` | **修复验收测试方案**：pytest / API / Playwright 三层用例矩阵、一键运行（`work/claude_test_and_review/_fix_verify_all.py`）、验收标准与已知限制 |
| `docs/20261106-任意格式文件下载实施记录.md` | **任意格式文件下载（最新，已实施）**：`workspace-file/-files/-zip` 三端点、FileChip/WorkspacePanel/步骤下载、Office 依赖、154 项单测 + 16 项真实 E2E 证据 |
| `docs/20261106-任意格式文件下载设计方案.md` | **任意格式文件下载设计**：workspace 通用下载端点、目录列表与打包、Markdown/工具步骤/文件面板三入口、安全边界与 P1–P5 实施测试计划 |
| `docs/20261106-Codex修复须知-Web自测Bug.md` | 自测 Bug 的修复建议单（含优先级、建议改法、验收与回归清单） |

### 0.3 目录约定（写文件前先确认放在哪个目录）

本项目顶层目录职责严格分开，**新建文件前先判断它属于哪一类，不要随手丢在根目录或 docs**。

| 目录 | 用途 | 放什么 / 不放什么 |
|---|---|---|
| `docs/` | **设计文档 + 进度记录**（长期留存、需要阅读） | 放：设计方案、实施文档、决策记录、changelog、进度记录、审计/调研报告。**只放 `.md`**。 |
| `misc/` | **零碎、不那么重要的内容** | 放：参考材料、截图、临时说明、外部粘贴文本（如 `*.txt`）、他人项目笔记。可读但与主线弱相关。 |
| `skill_assets/` | **Skill 交付包的本地目录（长期留存、不入库）** | 放：可导入的 `*.zip` 技能包，及其解包后的同名目录（作为可读的源）。**与 `work/` 的区别：这里的文件不会随时被清掉。** 整个目录已在 `.gitignore` 中排除（2026-11-11 决定）——技能包可能含业务数据或体积较大，不适合随仓库分发；需要分发时通过界面「导入技能包」传给对方。新增技能包时，可读源、zip、`backend/skills_shared/<name>/` 三处内容必须一致（`diff -r` 验证）。 |
| `work/` | **临时测试产物的存放目录** | 放：一次性脚本（`_*.py`）、日志（`*.log`）、探针、构建监视脚本、临时 dump、调试快照。**可随时清理，不入正式交付。** |
| `tools/` | **长期保留的运维/排查工具（入库）** | 放：需要反复使用的诊断脚本，如 `team_timeline.py`（从团队事件 JSONL 还原协作时间线）、`probe_chat_prelude.py` / `probe_soft_compact_blocking.py` / `probe_prelude_direct.py`（首帧串行阻塞三探针，见 `docs/20261112-Hlaudz首帧串行阻塞核实与优化设计.md` §5.3）。与 `work/` 的区别：这里的东西**会一直用**，不是一次性脚本。 |
| `backend/` `frontend/` | 产品代码 | 只放能跑的实现代码与资源；不要把测试产物、日志、截图塞进去。 |

> **Skill 三处存放的分工**（容易混淆，先说清）：
> - `backend/skills_shared/<name>/` —— 运行时真正加载的位置，改这里才影响 agent 行为；
> - `skill_assets/<name>/` —— 可读的交付源，与公用层内容一致，用作版本留档与再分发（**不入库**）；
> - `skill_assets/<name>.zip` —— 通过界面「导入技能包」分发给没有仓库写权限的人（**不入库**）。
>
> 注意：`skill_assets/` 整个目录已被 `.gitignore` 排除，所以 clone 仓库不会带上任何技能包。
> 依赖这些技能的环境要单独同步（或从 `backend/skills_shared/` 重新导入）。
>
> 改 skill 的顺序：先改 `skill_assets/<name>/` → `cp -r` 到 `backend/skills_shared/` →
> 重新打包 zip → `diff -r` 三处校验一致。

**docs 目录写法与格式要求**（重要）：

1. **文件名必须是 `日期-名字`**（`YYYYMMDD-中文简述`），例如 `docs/20261105-Skill与基础工具设计与实施.md`。
   禁止无日期的裸名字（如 `design.md`）；英文调研/外部资料可保留原名，但也应尽量带日期前缀。
2. **统一用 Markdown**，UTF-8 无 BOM、LF 换行。
3. **新文档建议结构**：`# 标题` → `> 一句话摘要/日期/状态` → 背景与目标 → 设计与决策 → 实施步骤 → 测试与验证 → 风险与未决项 → 变更记录。
4. **两类文档分开写**：设计与决策写在「设计文档」，每阶段完成情况写在「changelog / 进度记录」，二者互相引用，不要混成一篇。
5. **不覆盖前一阶段产物**：进度记录按阶段新建（或追加到最新 changelog），不改写历史文档的既有结论。
6. docs 里**不放图片/二进制/临时文本**——截图等放 `misc/`，需要引用时写相对路径 `misc/xxx.png`。

**work 目录使用约定**：

- 一次性脚本用 `_` 前缀（如 `work/_patch.py`），跑完即删，避免堆积。
- 日志统一放 `work/`（如 `work/backend_hot.log`），不要写进仓库源码目录。
- `work/` 内容视为**可丢弃**：任何需要长期保留的结论，必须落到 `docs/` 的文档里，而不是留在 work。

## 1. 环境铁律（Windows：外层 PowerShell，内层 Git Bash）

### 1.0 三行速查（每次调用 `exec_command` 前必须过一遍）

1. 工具参数固定 `shell="powershell"`、`login=false`；**`shell` 永远不填 `bash`**。
2. `cmd` 外层只能是 PowerShell 语法：先 `$script = ` + @' 起始 here-string，再 `& 'D:\Git\bin\bash.exe' -lc $script`。
3. 一切 Bash 语法（`export` / `&&` / `|` / `cat <<` / `for`）只能写在 here-string 内部，它们只是 `-lc` 的 payload。

> 判据：**外层 `cmd` 是 PowerShell，内层才是 Git Bash。**
> 一旦发现自己在想把 `bash` 填进 `shell`，或把 `&&` / `|` / `cat <<` 直接写进 `cmd` —— 停手，方向错了。

### 1.0.1 可复制模板（推荐多行写法，照抄不要改写）

```powershell
$script = @'
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
cd /d/git_codes/inner_chatgpt
# ... bash payload ...
'@
& 'D:\Git\bin\bash.exe' -lc $script
```

- 第一行 `$script = @'` 与结束行 `'@` 必须**各自独占一行**；`'@` 必须是行首字符，其后不接任何内容。
- `D:\Git\bin\bash.exe` 是本机实测路径；换机器先 `where.exe bash` 确认。

### 1.0.2 报错 → 诊断（出现下表现象，说明外层不是 PowerShell）

| 现象 | 真实原因 | 正确做法 |
|---|---|---|
| `/usr/bin/bash: line 1: =: command not found` | 整段 PowerShell 被当 bash 执行，外层是 bash/WSL | 改回 `shell="powershell"`，Bash 内容放进 here-string |
| `syntax error near unexpected token '&'`，且回显含 `& 'D:\Git\bin\bash.exe'` | 同上，调用行被 bash 解析 | 同上 |
| 自己把 `shell` 写成了 `bash` | 本机命中 WSL 存根，必错 | 改成 `shell="powershell"` |

> 注：输出里出现 `bash=/usr/bin/bash`、`MINGW64_NT-...` 是**期望结果**（子进程已是 Git Bash），不要误判成“外层成了 bash”。


### 1.0.3 写「含 PowerShell here-string 示例」的文档时

- here-string 的结束是**任意行首出现结束符两字符**即触发，哪怕它只是正文示例的一部分。
- 所以：**不要**在 payload 里直接写出“行首的结束符”；改用占位符，最后在 Python 里用 `replace` 将占位符还原成真实结束符。同理也不要把开头符写在行首示例里。
- 通用做法：写这类文档用独立 `.py` 脚本（raw 字符串 + 占位符替换），不用 heredoc 直接落盘。


- **命令内容在 Git Bash 子进程执行，但工具外层永远不是 Bash。** `exec_command` 的默认 shell 是 **PowerShell**；**禁止使用 `shell="bash"`**，本机该值可能命中 WSL 存根，并且会把下面的 PowerShell wrapper 当成 Bash 语法而报错。唯一允许的调用骨架是：

  ```text
  exec_command(
    cmd="& '<Git Bash 可执行文件绝对路径>' -lc '<bash-payload>'",
    shell="powershell",
    login=false,
    workdir=...
  )
  ```

  多行 payload 用 PowerShell 单引号 here-string 传入：

  ```text
  $script = @'
  ... bash payload ...
  '@
  & '<Git Bash 可执行文件绝对路径>' -lc $script
  ```

  `$script = @'`、`'@` 和开头的 `&` 都是 **PowerShell 语法**，不能作为 `shell="bash"` 的 `cmd`。看到 `export`、heredoc、`bash $'\r'`、`&&`、管道等 Bash 语法时，也只说明它们是 `-lc` 的内部 payload，绝不代表应把工具参数改成 `shell="bash"`。

  - ❗ 不显式传参时走的是 **PowerShell**（`PSVersion=...`）——这就是之前“嘴上说 Git Bash、实际跑 PowerShell”、以致中文与 heredoc 报错的根因。**不要依赖默认值，也不要只“声称”在 Git Bash。**
  - ⚠️ **本机实测（2026-10-19）**：`shell="bash"` 会命中 **WSL 存根**，不是 Git Bash；即使某次没有命中 WSL，也会把 PowerShell wrapper 当成 Bash 语法并报 `syntax error near unexpected token '&'`。两种情况都不得改用 `shell="bash"`。
  - 🛡 Git Bash 绝对路径兜底：先用 `where.exe bash` / `where.exe git` 确认本机安装位置，再用 PowerShell 包装器调用，最确定：

    ```powershell
    & '<Git Bash 可执行文件绝对路径>' -lc '<script>'
    ```

  - 📍 **Git Bash 路径是机器相关的**：本机实测 `D:\Git\bin\bash.exe`，可直接用于 §1.0.1 模板；**换机器先 `where.exe bash` / `where.exe git` 确认**，其他机器用 `where.exe bash` 自行探测确认。gitconfig / node 路径同理以本机 `where.exe` 结果为准。

  - ✅ 判定是否真的进入 Git Bash：输出应包含 `bash=/usr/bin/bash`、`MINGW64_NT-...`、`LANG=C.UTF-8`；不要只看命令是否返回 0。
- 路径映射：盘符 `X:\x` → `/x/x`（如 `D:\x` → `/d/x`）。
- **路径格式不可混用**：Git Bash 内用 POSIX 路径（`<盘符>/...` 或相对路径）；Windows 程序（`python` / `node`）**不认** POSIX 路径，要传原生路径（`X:\...` / `X:/...`）。同一条命令里不要混用两种。
- Python 用 **`python`**（不是 `python3`，后者命中 WindowsApps 存根）。
- Node 用 `node`（本机实测 v25.2.1；路径与版本因机器而异，以 `where.exe node` / `node --version` 为准）。
- Git Bash 的 `find -printf` 可能不可用；没有 `strings` 命令（用 `grep -a`）。
- 写含 `$` 的文件时，用**单引号** here-string（`@'...'@`），否则 `$` 会被 PowerShell 展开。
- **git 行尾**：系统级 gitconfig（`git config --list --show-origin` 可查位置）里 `core.autocrlf=true`（Git for Windows 安装器默认），会让 `git add` 刷 LF/CRLF 警告、`git status` 出幽灵 diff。**本仓库已用仓库内 `.gitattributes`（`* text=auto eol=lf`）统一为 LF 根治**——只影响本仓库，不动系统级 `core.autocrlf=true`。**遇到行尾/CRLF 警告不要再排查，`.gitattributes` 已统一解决。**
- **写文件一律 LF**：编辑/生成任何文本文件都直接写成 LF（`\n`），不要 CRLF。

  - 仓库已用 `.gitattributes`（`* text=auto eol=lf`）在入库时自动规范化。**看到"整文件 diff"先怀疑行尾，一步确认即可，
    不要展开排查、不要反复 checkout/重写** —— 那是纯 token 浪费。
  - 程序化写文件的坑（本机实测）：**Python `open(p,"w")` 在 Windows 会把 `\n` 翻成 `\r\n`**
    → 必须 `open(p,"w",newline="\n")`（或写二进制）；Node `fs.writeFileSync` 默认原样写，确保字符串里是 `\n`。
  - 快速校验（期望 0）：`python -c "import sys; print(open(sys.argv[1], 'rb').read().count(b'\r'))" '<file>'`
- **md / 文档命名**：新建 markdown 及文档时，文件名一律用 `日期-名字` 格式（日期 `YYYYMMDD`），例如 `docs/20261012-研究workflow.md`；不要用无日期的裸名字。

- **长文本写入必须分段（Windows / Codex 实测）**：
  - Windows `CreateProcessW` 的命令行硬上限是 **32767 UTF-16 code units**；本机 `bash -c` 脚本体实测最大 **32735**，32736 报 `WinError 206`。
  - 但 Codex tool-call 参数会在远低于 OS 上限时出现截断；本项目实际出现过约 3.1 万字符 / 4 万 UTF-8 字节的 heredoc 只写入前半段，末尾 `EOF` 未到达。
  - 因而禁止把整篇长文塞进一次 `exec_command`。**中文正文每段约 2K-4K 字符，ASCII/代码每段不超过 8KB；单条命令目标总长 ≤ 8KB，绝不超过 12KB。**
  - 长文第一段用 `cat > file <<'MDOC_20261021_A7F3'`，后续段用 `cat >> file <<'MDOC_20261021_A7F3'`；每次都使用**本次唯一的 delimiter**，并在正文结束后用完全相同的单独一行闭合。
  - **禁止在包裹长文时使用裸 `EOF`。** 引号 heredoc（`<<'EOF'`）只阻止变量展开，不能阻止正文里单独一行的 `EOF` 提前结束 heredoc；写“包含 heredoc 示例的 Markdown”时，外层 delimiter 必须不同于所有内层示例 delimiter。
  - 按 Markdown 章节边界拆，不拆 code fence、JSON object、表格行或源码片段的中间；每段独立闭合 heredoc。
  - 看到 `here-document ... delimited by end-of-file (wanted ...)` 时，先检查是命令截断还是正文中出现了同名的 delimiter 行：检查文件尾部与 delimiter 出现位置，从最后完整章节继续，换一个新的唯一 delimiter，并缩小下一段；禁止原样重试同一大命令。
  - 上述限制均为本机实测结论，直接照做即可。

- **heredoc 只用于「写死内容」，不用于「跑逻辑」（2026-10-26 补充）**：
  - 判定标准只有一条：**这段内容是不是原样落盘**。是 → 可以用 heredoc；需要循环、正则、读现有文件、条件分支、多步改动 → **一律写成独立 `.py` / `.mjs` 脚本再执行**，禁止用 `python - <<'X'` 跑脚本。
  - ❌ 反面例（本仓库实际踩过）：`python - <<'PY_X' && echo done` —— 正文和闭合行都没写，bash 报 `here-document ... delimited by end-of-file`，`python -` 读到空 stdin 进入 REPL，后面的 `&& echo` 照样执行，于是**既没改文件也没报错**。
  - ✅ 正面例（插入表格行 / 改行 / 条件写入这类“要判断”的活）：

    ```bash
    cat > work/_patch.py <<'PY_PATCH_20261026_0713'
    import sys
    path = sys.argv[1]
    text = open(path, encoding="utf-8", newline="").read()
    if "已插入" in text:
        print("skip: already patched")
        sys.exit(0)
    ...  # 真实逻辑
    PY_PATCH_20261026_0713
    python work/_patch.py AGENTS.md
    ```

  - 闭合行必须**独占一行且后面不接 `&&` / `;` / `|`**；命令里出现第二个命令时，把 heredoc 段写成单独一条 `exec_command`。
  - **heredoc 正文里不要出现“单独一行的 delimiter 候选”**，尤其别写 `EOF`、`END`、`DONE`、`PY`、`PYTHON` 这类通用词；统一用 `用途_日期_随机4位`，如 `PY_PATCH_20261026_K3M9`。
  - 每条一次写入的正文 ≤ 2K-4K 中文字符 / ≤ 8KB ASCII；超了就按 Markdown 章节或函数边界拆成多条 `cat >>`，每条独立闭合。
  - 需要写“包含 heredoc 示例的 Markdown”时，**外层 delimiter 必须与正文里所有内层 delimiter 不同**，且外层不要用 `MD`、`EOF` 这类会被示例正文命中的词。
  - **自查（写完必跑一条）**：`tail -c 200 <file>` 看结尾是否完整；`grep -c $'\r' <file>` 期望 0；含中文再 `python -c "open('<file>','rb').read().decode('utf-8')"`。
  - **写坏后的恢复顺序**（不要原样重试同一条命令）：① 看 `tail -c 300 <file>` 判断是“截断”还是“正文里撞了 delimiter”；② 若是截断，从最后一个完整章节/函数边界继续，换新 delimiter、缩小下一段；③ 若是撞 delimiter，只重写被截断的那一段；④ 逻辑类命令直接删掉 heredoc，改独立脚本重做。


---

## 2. 真实 e2e 验证（强制）

**铁律：单测绿 ≠ 链路通。** `pytest backend/tests` 只证明分支逻辑正确；
「消息有没有送达、子代理有没有被回收、前端能不能看到」必须用真实 LLM 跑一遍。

GA 项目在单测全绿时用真实 e2e 抓出 12+ 个链路缺陷（关闭竞态、僵尸 running、
任务悬挂、通知丢失），全部是单测**原理上**测不到的。Hlaudz 很可能有同类问题。

### 2.1 什么时候必须跑（缺一不可）

1. 改了 `backend/app/subagent/`、`team_host.py`、`team_store.py`、`team_tools.py`；
2. 改了 `backend/app/builtin_tools.py` 或新增/删除工具；
3. 改了 `agent.py` 的 `SYSTEM_PROMPT`、`subagent/prompts.py`、skill 索引；
4. 改了 `routers/chat.py` 的 SSE 流、停止、回收、事件合并；
5. 修的是链路级缺陷（通知丢失、僵尸状态、关闭竞态、任务悬挂）；
6. 要声称某个 UX 行为「用户能看到」；
7. 用户说「测一下 / 验证一下 / 看看效果」。

### 2.2 怎么跑

**必须先读 `.claude/skills/hlaudz-e2e/SKILL.md`**，按它的场景矩阵（S0–S9）挑用例。
最少跑 **S1（subagent 基础）+ S5（team 基础）+ S0（负对照）**。

三条不可省略的条件：

- **真实 LLM**：拿假 LLM 跑出来的结果属于 L2，不能当链路证据；
- **唯一 runId + 事件落盘**：`backend/tests/e2e/runs/<runId>/events.jsonl`；
- **哨兵 token**：派活时塞随机串，要求下游复述。没复述 = 没送达。

### 2.3 三层各证明什么

| 层 | 命令 | 证明 |
| --- | --- | --- |
| L1 单测 | `pytest backend/tests -q` | 分支、边界、CAS、权限 |
| L2 集成（假 LLM） | 同上，脚本化 `tool_calls` | 编排顺序、事件形状 |
| L3 真实 e2e | `python -m backend.tests.e2e.run --case S1` | 链路能跑通、模型真的这么用 |

**三层全绿才叫完成。** L1 绿 + L3 红 = 有链路缺陷；L3 绿 + L1 红 = 有边界缺陷。

### 2.4 判据三级，措辞不许混

| 级别 | 证据 | 允许说 |
| --- | --- | --- |
| L1 构造得出 | 代码路径存在 | 「机制已实现」 |
| L2 运行时发了 | 事件流里出现 | 「机制已触发」 |
| L3 用户看到了 | 前端渲染 / 截图 / 持久化消息 | 「UX 已修复」 |

只有 L3 能说「用户能看到」。UI 改动必须另跑 Playwright 截图，
或至少核对 `GET /api/sessions/{sid}/messages` 的持久化内容。

### 2.5 报告必须带现场

每次 e2e 结论至少给：**runId、工具调用序列、失败的断言、事件 seq、现场路径**。
没有这三样，缺陷无法复现，等于没测。

### 2.6 与 §8.5 TDD 的衔接

- 单测/集成仍按 §8.5 的 RED → GREEN → REFACTOR，且必须做「stash 实现后重新变红」；
- e2e 不参与红绿灯（跑一次分钟级），但**修链路缺陷时**要先有一条能稳定复现的 e2e，
  再改代码；改完连跑 3 次无 flake 才算修好；
- e2e 跑出的缺陷，落地成 L1/L2 用例后才进回归集——否则下次还会漏。

### 2.7 明确不做

- 不把 e2e 塞进 CI（需要真实 LLM 额度，耗时不可控）；
- 不 mock LLM 冒充 e2e；
- 不以「模型这次没这么做」判定产品缺陷——同一 prompt 连跑 3 次再下结论。
