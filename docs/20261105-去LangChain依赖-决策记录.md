# 决策记录：去除 LangChain / LangGraph，改用 GenericAgent 式自研内核

- 日期：2026-11-05
- 状态：**已决定，P0 已实施（2026-11-05，见 `docs/20261105-changelog-P0.md`）**
- 决策人：项目负责人
- 关联文档：
  - `docs/20261105-LangChain-LangGraph依赖审计.md`（现状审计）
  - `docs/20261105-去LangChain去LangGraph解耦方案.md`（可行性评估）
  - `docs/20261105-去LangChain依赖-实施文档.md`（本文的执行蓝本）

---

## 1. 背景

本项目当前代码是从 `D:\git_codes\jiakuan_langchain` 复制来的快照，后端基于
LangChain 1.4.8 + LangGraph 1.2.19 搭建。审计结论（见审计文档）：

- 真正 import 这两个库的只有 7 个文件、约 12 行；
- 这两个库只承担 6 项能力（模型 HTTP、agent 主循环、会话持久化、流式事件、工具抽象、消息基元）；
- 项目为了用这两个库，反而自研了大量代码去绕开它们（压缩、token、悬空 tool_call 自愈、状态替换）；
- 这两个库**没有**提供 subagent / 多智能体，而 `D:\git_codes\GenericAgent`（零 langchain 依赖）
  已经用更简单的方式实现了这些能力。

同时，用户已在 `D:\git_codes\GenericAgent` 长期维护一套成熟的手写 agent 内核，可作为参照实现。

## 2. 决策

1. **移除 langchain / langgraph 及其 checkpoint 依赖**（`langchain`、`langchain-core`、
   `langchain-openai`、`langgraph`、`langgraph-checkpoint-sqlite`）。
2. **用 GenericAgent 的设计方式（不是直接 import GA 代码）自研替换**：
   - 自研模型客户端（httpx，支持 `openai_chat` / `openai_responses` 两线 + 流式）；
   - 自研 agent 主循环（显式 while + 工具 dispatch + 事件 yield）；
   - 自研会话历史存储（单一权威历史，不再双写 checkpoint）；
   - 自研压缩（把现有 `compaction.py` 改成操作自有 history）。
3. **本轮只做「轻量化基线替换」**：保住现有基本功能，不新增高级特性。
4. **浏览器控制暂时不做**（GA 的 `web_*` 工具不移植）。
5. **保留并沿用**：多用户/鉴权/JWT、SQLite 业务表、MCP 接入（fastmcp）、SSE 事件协议、前端。

## 3. 本轮范围（轻量化基线）

必须等价保留的现有功能：

| # | 功能 | 说明 |
|---|---|---|
| 1 | 多用户注册/登录、会话隔离 | 完全不动（auth/db/routers） |
| 2 | 会话 CRUD、消息持久化、resume | 持久化改自有存储，接口不变 |
| 3 | 流式对话（文本 + 思考 + 工具卡片 + 图表 artifact） | SSE 事件协议**保持不变** |
| 4 | 停止生成 | 保留 stop_event 机制 |
| 5 | 会话级模型切换 + 用户级 LLM 配置 | 保留，换成自研客户端 |
| 6 | 用户级 MCP 配置 + 标准 mcp.json 导入 | 完全不动 |
| 7 | 手动压缩 / 软线自动压缩 / 硬线裁剪 | 保逻辑，换成操作自有 history |
| 8 | 悬空 tool_call 自愈 | 保留（改在自有 history 上做） |

明确**不在本轮**（后续强化）：

- 浏览器控制（web_scan / web_execute_js 等）——**不做**；
- 代码执行（`code_run`）、文件编辑（`file_read/write/patch`）——后续；
- 一整套 subagent / team / task 工具——后续；
- 计划模式（plan mode）——后续；
- goal 机制——后续；
- hooks 调度——后续。

## 4. 决策理由

1. **依赖负担与收益不匹配**：5 个顶层包 + 20 余传递依赖，换来的只是一个 HTTP 客户端、
   一个 while 循环、一个 JSON 存储，且都已被证明可以手写。
2. **框架已被绕开**：压缩、token、自愈、状态替换全是自研，框架的「增值」没兑现。
3. **技术路线统一**：用户已决定把本项目发展为「GenericAgent 的 Web 多用户版」，
   继续保留 langchain 会在后续加 subagent/team/goal/hooks 时形成架构冲突。
4. **有成熟参考答案**：GA 的 `llm/core.py`、`core/loop.py`、`core/context.py`、`agents/` 已提供设计蓝本。
5. **风险可控**：改造面被限制在 `backend/app` 的少数文件；多用户/鉴权/MCP/前端不动。

## 5. 关键约束（铁律）

1. **不直接 import GenericAgent 包**。只借鉴设计，重写成本项目自己的模块，避免带入 GA 的 CLI 耦合
   （`handler.py` 约 13.6 万字符、`app.py` 约 14.6 万字符）。
2. **SSE 事件协议与前端契约保持不变**（`text_delta/thinking_delta/tool_start/tool_result/chart/
   compaction/done/error/message_start/healed`），前端本轮尽量零改动。
3. **对外 HTTP API 路径与响应结构保持不变**。
4. **写文件一律 UTF-8 / LF**；中文内容用 heredoc 分段写入。
5. **每阶段独立可验证**，允许新旧实现并存，用开关切换，验证通过再删旧代码。
6. **不删历史数据**：迁移前先备份 `backend/data/app.db`（含 `-wal/-shm`）。

## 6. 验收标准（Definition of Done）

- `backend/requirements.txt` 不再出现 langchain / langgraph；
- 全仓 `grep -rn "langchain\|langgraph" backend --include=*.py` 只剩注释性文字或为零；
- `pip install -r backend/requirements.txt` 能从干净环境装好；
- 后端能启动、登录、建会话、多轮对话、流式、工具调用、压缩、停止、删除、resume 全部正常；
- 老会话（迁移后）能打开、能继续对话；
- 前端无需改动即可工作（除可选的 localStorage key 更名）。

---

## 7. 库 API → 自研实现 对照表（决策级）

| 现用库 API | 位置 | 替换为（自研） | 备注 |
|---|---|---|---|
| `langchain_openai.ChatOpenAI` | `llm.py` | `llm_client.py::LLMClient`（httpx） | 支持 chat/responses 两线、流式、reasoning |
| `create_agent(...)` | `agent.py:148` | `agent_loop.py::AgentLoop` | 显式 while + tool dispatch |
| `agent.astream_events(v2)` | `sse.py:91` | `AgentLoop` 直接 `yield` 事件 | 事件名保持前端契约 |
| `StructuredTool.from_function` | `mcp_client.py:163` | `tools.py::Tool`（name/desc/schema/`__call__`） | 工具注册表化 |
| `HumanMessage/ToolMessage/RemoveMessage` | `compaction.py`/`agent.py` | 纯 dict 消息（`{"role","content","tool_calls","tool_call_id"}`） | 去掉库消息类型 |
| `agent.aget_state` / `aupdate_state` | `compaction.py`/`agent.py` | `history_store.get/set(session_id)` | 单一权威历史 |
| `REMOVE_ALL_MESSAGES` | `compaction.py` | 直接 `history[:] = [...]` | 整段替换 |
| `AsyncSqliteSaver`（checkpoints/writes） | `runtime.py:33` | `history_store` + 现有 `messages` 表 | 一次性迁移历史 |
| `saver.adelete_thread` | `sessions.py:77` | `history_store.delete(session_id)` | 跟随删除会话 |

## 8. 长线路线图（本轮之后）

用户明确后续要「继续强化本项目」，把 GA 的高级特性搬入 Web 多用户形态。分阶段：

| 阶段 | 内容 | 前置 | 参考 GA |
|---|---|---|---|
| P0（本轮） | 去 langchain；轻量化基线（对话/工具/压缩/持久化） | — | `llm/core.py`、`core/loop.py`、`core/context.py` |
| P1 | 代码执行 `code_run`（沙箱、超时、截断） | P0 | `core/handler.py::do_code_run`、`code_run()` |
| P2 | 文件编辑 `file_read/file_write/file_patch` | P0 | `core/handler.py::do_file_*` |
| P3 | 子代理 `spawn/list/wait/resume/interrupt/close` | P1/P2 | `agents/manager.py` |
| P4 | 团队/任务板 `team_status`、`task_create/get/update/list`、消息 | P3 | `agents/team.py`、`agents/tasks.py`、`agents/mailbox.py` |
| P5 | 计划模式 plan mode | P2 | `core/handler.py::enter_plan_mode` |
| P6 | goal 机制（goal_proposal/evaluator/wake/nudge） | P3 | `core/goal*.py` |
| P7 | hooks 调度（SessionStart/Stop/PostToolBatch 等） | P3 | `genericagent/hooks/` |
| P8（可选） | 浏览器控制 web_scan/web_execute_js | — | `core/handler.py::do_web_*`、`web/` |

> **架构提醒（重要）**：GA 的 subagent 基于「独立 OS 进程 + 文件 IPC」（`subprocess.Popen` +
> `mailbox.jsonl`）。Web 多用户环境应改为「进程内 asyncio task 池」或「独立 worker + 队列」，
> **不能照搬进程模型**。这一点在 P3 开工前必须重新设计，届时单独出方案文档。

## 9. 决策后的仓库形态

- 本项目从此不再是「langchain 版 demo」，而是「GA 内核的 Web 多用户实现」。
- 因此 `AGENTS.md`、`README.md`、`docs/20261104-*.md` 里大量「LangGraph/LangChain 技术栈」
  表述将在 P0 完成后统一修订（本轮先不动这些文档，避免与正在进行的实施冲突）。
- 建议 P0 完成后新增一条 changelog 并更新 README 的技术栈段落。
