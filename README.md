# Hlaudz — 多用户 Web Agent 平台（自研 Agent 内核）

> **一句话**：Hlaudz 是多用户、会话隔离的 Web Agent 服务 —— FastAPI + React，**不依赖 LangChain / LangGraph**，
> 支持用户自带模型、MCP 工具、Skill、子代理（Subagent）、Agent Team 协作、产物文件下载与 SSE 流式输出。

> **文档快照**：2026-11-09 · 代码基线 `main@3b9c68f`（P3 + 文件下载 + Web 自测修复已完成）

---

## 1. 能力一览

| 能力 | 现状 |
|---|---|
| 账号与隔离 | 邮箱或用户名 + 密码登录（bcrypt）；JWT access token + HttpOnly refresh cookie 旋转；会话/消息/模型/MCP/Skill 全部按 `user_id` 隔离，越权返回 404 |
| 模型接入 | 每个用户自配 `base_url / api_key / model / wire_api`，互不可见；支持 `openai_chat` 与 `openai_responses` 两线；会话级模型切换（`sessions.model_id`） |
| Agent 内核 | 自研显式工具循环（模型 → 工具 → 回填 → 再模型），单轮最多 300 轮；悬空 tool_call 自动自愈；工具错误回喂给模型自纠 |
| 会话持久化 | SQLite 双轨：`session_history` 为模型可回放历史，`messages` 为前端展示快照；支持 resume / 删除 / 重命名 / 置顶 |
| 上下文压缩 | 发送前软线自动摘要（CJK-aware token 估算）+ 手动压缩 + 硬线裁剪兜底；保留 tool_call/tool 配对 |
| MCP | 标准 `mcp.json`（`mcpServers`）导入导出；`http / sse / stdio`；多服务器聚合、单个失败降级跳过 |
| Skill | 三层可见性：内置 `skills_builtin/`、公用 `skills_shared/`、用户私有 `data/skills/<user_id>/`；zip 目录包安全导入；system prompt 只放 L1 清单，正文由 `load_skill` 按需加载 |
| 内置工具 | `file_read / file_write / file_patch / list_files / code_run / load_skill`，读写限制在个人 workspace |
| 子代理 Subagent | 进程内 asyncio 运行时：`spawn_agent / wait_agent / read_agent_result / list_agents / close_agent`；子代理继承全部业务工具与 MCP，但不能派生子代理；事件并入主对话 SSE |
| **中途插话（steering）** | **生成过程中可以继续输入**：按发送不会另起一轮，而是插进正在执行的那个回合，模型在下一次模型调用前读到。后端 `POST /messages` 的 `mode:"queue"` 返回 `202 {"queued":true,"queue_depth":n}`；前端显示「已插入当前任务」角标。队列按 generation 存活（跨 fold 点不丢），单条上限 4000 字符、队列深度 20。详见 `docs/20261111-用户中途插话steering实施方案.md` |
| Agent Team | 持久任务板 + 依赖 `blocked_by` + CAS 版本 + 团队消息 + lead/teammate 角色；右侧 `TeamPanel` 实时展示任务、消息与动态；lead 8 个编排工具，teammate 5 个 |
| 外部 GenericAgent（ACP） | 以 ACP 拉起本机 GenericAgent 子进程作为外部编码代理：`ga_spawn / ga_wait / ga_result / ga_send / ga_stop` 五个 root-only 工具，事件并入主对话 SSE（`subagent_*`）。**manager 级真 GA E2E 已通过**：预检 + 工具注入、哨兵写文件、多步（读 → 改 → 跑，3 个工具步骤）、长任务停止与停止后立刻继续；预检用 `importlib.util.find_spec` 解析路径、不 import bridge（避开其 import 期 stdout 重定向），真机返回 site-packages 路径与 SHA256。Web UI E2E 未跑，DB blocks / 卡片渲染与 chat 层「停止后立即发送」未在该层验证；详见 `docs/20261109-ACP-client驱动GenericAgent实施记录.md` |
| A2A 接入 | 每用户独立 `agent_id` / URL / Bearer token；JSON-RPC `SendMessage` / `SendStreamingMessage` (SSE) / `GetTask` / `ListTasks` / `CancelTask` / `SubscribeToTask`；默认关闭（`INNER_A2A_ENABLED=true` 才暴露，实例级硬闸）；用户级开关默认关、可在设置页热切换；token 级能力档位 `full` / `read_only` / `chat_only`（建 token 时定死，运行期不弹审批）；并发配额 + 审计表；管理入口「设置 → A2A 接入」；详见 `docs/20261110-A2A-MVP实施记录.md` |
| 图片输入 | 选图/粘贴/拖拽上传，图片落在 `<workspace>/uploads/`，DB 只存引用；送模型时转 `image_url` / `input_image` 数据块（`openai_chat` 与 `openai_responses` 两种线格式都支持）；最近 3 轮图片在多轮对话中保留 |
| 文件产物 | 任意格式下载：单文件、目录浏览、多选打包 zip；Markdown 本地文件胶囊、工具步骤行下载、右侧工作区面板三入口；Office 文档生成依赖已内置 |
| 停止可靠性 | 协作停止旗 + `task.cancel` + 子代理/`code_run` 级联三层；陈旧 generation 租约自动收敛，停止后已生成内容保留 |
| **韧性（API 中断自愈）** | 上游 502/504/429/断流自动恢复，三层兜底：**L1 调用层**（退避重试 + 尊重 `Retry-After` + 断流检测 + 降级非流式重取）、**L2 循环层**（模型调用彻底失败后有限续跑，只重发模型调用、不重跑工具）、**L3 团队层**（lead 想收工时队友还在跑则不许结束；队友异常结束自动释放它占的任务；`reclaim` 幂等、`create_task` 同名去重）。上游彻底不可用时追加一段「已完成工作」摘要收尾（纯读库生成，不调模型）。详见 `docs/20261111-AgentTeam韧性设计与实施.md` |
| **可观测性** | **默认开启**：应用日志 `INFO` 落 `backend/data/logs/app.log`；团队协作事件单独写 `team-<日期>.jsonl`（每行一条 JSON，可直接 grep `message_id` / `agent`）；`tools/team_timeline.py` 把散落事件还原成人读时间线并自动诊断消息滞留/投递失败/中断自愈情况 |
| 前端 | React 18 + Vite + TypeScript；暖纸 + 陶土设计系统；Markdown/GFM、图片鉴权渲染、ECharts 图表、子代理卡片、团队面板、文件面板 |

> 界面示意：同一会话内 Agent 先查数（工具步骤）再画图（ECharts，可下载 PNG），
> 多轮对话与图表一起持久化；左侧会话列表按用户隔离，右侧可展开团队面板与文件面板。

---

## 2. 技术栈

- **后端**：FastAPI + 自研 Agent 内核（无 LangChain / LangGraph）
  - `llm_client.py`：httpx 直连模型（chat / responses 双线 + 流式 + reasoning）
  - `agent_loop.py`：显式工具调用循环与事件流
  - `tools.py`：自研 `Tool` / `ToolRegistry` 抽象
  - `history_store.py`：`session_history` 单表历史
  - `compaction.py` + `tokens.py`：软线摘要 / 硬线裁剪 / token 估算
  - `builtin_tools.py` + `run_guard.py`：内置文件与代码工具 + workspace 写入守卫
  - `subagent/`：进程内 asyncio 子代理运行时（context / store / events / capabilities / registry / runtime / host / tools / prompts / session）
  - `team_store.py` / `team_host.py` / `team_tools.py` / `team_resume.py`：Agent Team 持久化、宿主、工具与跨轮恢复
  - `quota.py`：全局 / 用户 / 团队 / 代理四级容量闸门
  - `runtime.py` + `process_registry.py`：Runtime 缓存、停止登记、活跃 `code_run` 进程登记
- **认证**：bcrypt + PyJWT access token + HttpOnly refresh cookie（旋转）
- **存储**：SQLite（`backend/data/app.db`，业务表 + 会话历史 + 子代理侧链 + Team 事实表同库）
- **前端**：React 18 + Vite + TypeScript，SSE 流式渲染，ECharts
- **MCP**：`fastmcp` / `mcp` 客户端，独立 server 模块可作为内置示例

---

## 3. 开发者必读

1. **项目交接与阶段状态**：[`AGENTS.md`](AGENTS.md)（**开工前先读**）
2. **功能实现原理**（主循环 / 双轨持久化 / 压缩 / 模型切换 / MCP / Skill / workspace / Subagent / Team / SSE）：
   [`docs/20261106-项目功能实现原理.md`](docs/20261106-项目功能实现原理.md)
3. **全部设计、实施、审计与 changelog**：[`docs/`](docs/)
4. **本次文档刷新记录**：[`docs/20261109-项目现状与文档刷新.md`](docs/20261109-项目现状与文档刷新.md)
5. **A2A 使用文档（含本机验收实例）**：[`docs/20261110-A2A使用文档.md`](docs/20261110-A2A使用文档.md)

---

## 4. 环境要求

- Python 3.12（命令用 `python`）
- Node.js 20+（已验证 v24 / v25）
- 依赖见 `backend/requirements.txt`（版本已固定）与 `frontend/package.json`

> **数据库、`.env` 与 `llm.yaml` 均为 Hlaudz 运行时数据，不入库。**
> - 干净 clone 后首次启动会自动创建空的 `backend/data/app.db`（无用户/会话）。
> - `.env` 放运行期环境变量（JWT 密钥、A2A/ACP 开关、模型 key 等），模板见 `.env.example`。
> - `llm.yaml` 是**可选**的 Hlaudz 兜底模型配置，模板见 `llm.yaml.example`。
>   缺失也能正常用——每个用户在「设置 → 我的模型」里各自配即可。
> - 模型 key 由每个用户在页面里自行填写；仓库不携带任何 key。

---

## 5. 干净 clone 后启动（首次）

```bash
# 1) 后端依赖
cd backend
pip install -r requirements.txt

# 2) 配置环境变量（推荐用 .env，不再靠每次启动前手动设）
cd ..
cp .env.example .env          # PowerShell: Copy-Item .env.example .env
# 按需编辑 .env：APP_JWT_SECRET / INNER_A2A_* / INNER_ACP_* 等

# 2b) 可选：配置兜底模型（没有也能用，用户在页面自己配）
cp llm.yaml.example llm.yaml  # PowerShell: Copy-Item llm.yaml.example llm.yaml
# 填 api_key，或把 ZhanLu_API_KEY 写进 .env 让它插值

# 3) 前端构建（产物由后端单端口托管）
cd frontend
npm install
npm run build

# 4) 启动后端
cd ../backend
python run.py
```

**环境变量优先级：真实环境变量 > `.env` > 代码内默认值。**
临时覆盖仍然有效（`$env:FOO="x"; python run.py` 会盖过 `.env`），CI 不受影响。

`.env` 在仓库根目录，由 `backend/app/__init__.py` 在**任何子模块导入前**加载——
这不是随手放的位置：`app.quota.Limits`、`app.auth.SECRET`、`app.acp.config` 都在
**导入时**读取环境变量求值，加载晚一步就会出现「配了不生效」。
`.env` 已被 `.gitignore` 排除（含密钥），`.env.example` 只放键名和说明、需要入库。

首次启动会自动创建空的 `backend/data/app.db`（无任何用户/会话）。

1. 浏览器打开 `http://127.0.0.1:8000/`，点「注册」创建账号：邮箱 + 密码，可选填**用户名**（不填则默认取邮箱 @ 前缀）。之后**邮箱或用户名都能登录**。
2. 打开「设置 → 我的 APIKey」，填 `base_url` / `api_key` / `model`（可选 `wire_api`），勾选「默认」并保存。
3. 回到会话页，新建会话即可对话。
4. 需要工具能力时，在「设置 → 我的 MCP」粘贴 `mcp.json`；需要技能时在「设置 → 我的 Skills」导入 zip 或新建。

- 健康检查：`GET http://127.0.0.1:8000/api/health`（`model` 为空表示没配 `llm.yaml` 兜底，属正常——用户在页面配了就能用）
- 若 `frontend/dist` 存在，后端会自动在 `/` 托管前端（单端口访问）。
- A2A 需要实例级开关：按下方「后端启动（日常）」带环境变量启动，再登录到「设置 → A2A 接入」打开你自己的开关并创建 token。

### 模型兜底 `llm.yaml`（可选）

**优先级：用户在「设置 → 我的模型」里配的 > `llm.yaml` 兜底。**
没配这个文件也能正常用，只是新用户必须自己填 key。

结构是 `providers → models → profiles → active_profile` 四层：

```yaml
active_profile: default

providers:
  zhanlu:
    base_url: https://ecloud.10086.cn/api/query/aigateway/v1
    api_key: ${ZhanLu_API_KEY}      # 从 .env 插值，避免明文落盘
    wire_api: openai_chat           # 或 openai_responses

models:
  deepseek-flash:
    provider: zhanlu
    api_model: moma/deepseek-latest-flash
    context_window: 400000          # 决定压缩软线/硬线
    max_tokens: 15920
    temperature: 1.0
    read_timeout: 120

profiles:
  default:
    model: deepseek-flash
```

`api_key` 支持 `${ENV_VAR}` 插值（见 `backend/app/config.py` 的 `_interp()`），
所以推荐把真实 key 写进 `.env`，`llm.yaml` 里只留 `${ZhanLu_API_KEY}`。

### 后端启动（日常）

**推荐用 `.env`**（配一次，之后重启都不用手动设）。

首配一次：

```bash
cp .env.example .env      # PowerShell: Copy-Item .env.example .env
```

`.env` 示例（只列常用的，完整清单见 `.env.example`）：

```ini
INNER_A2A_ENABLED=true
INNER_A2A_PUBLIC_BASE_URL=http://127.0.0.1:8000
INNER_ACP_GA_PYTHON=C:/anaconda3/python.exe
INNER_ACP_MODE=full_access
```

之后每次启动只要一行（`.env` 会被自动加载）：

```powershell
cd D:\git_codes\inner_chatgpt\backend; python run.py
```

**仍然可以临时覆盖**（真实环境变量优先于 `.env`）：

```powershell
cd D:\git_codes\inner_chatgpt\backend
$env:INNER_A2A_ENABLED="true"
$env:INNER_A2A_PUBLIC_BASE_URL="http://127.0.0.1:8000"
python run.py                        # http://127.0.0.1:8000
```

存成一行（新开终端时环境变量不会保留，所以每次都要带上）：

```powershell
cd D:\git_codes\inner_chatgpt\backend; $env:INNER_A2A_ENABLED="true"; $env:INNER_A2A_PUBLIC_BASE_URL="http://127.0.0.1:8000"; python run.py
```

Linux / macOS / Git Bash 同理：

```bash
cd backend
INNER_A2A_ENABLED=true INNER_A2A_PUBLIC_BASE_URL=http://127.0.0.1:8000 python run.py
```

三个要点：

1. `INNER_A2A_ENABLED=true` 是**实例级硬闸**——不设就完全没有 A2A 端点（AgentCard、JSON-RPC、token 管理一律 404）。
2. 它只是「允许开」，**用户级开关默认关**：登录后到「设置 → A2A 接入」点一下才会真正响应外部调用；随时可关，**不用重启**。
3. `INNER_A2A_PUBLIC_BASE_URL` 必须是外部客户端实际能访问到的地址（反代后的域名 / 内网地址），它会写进 AgentCard 和 token 返回的 URL。

不需要 A2A 时，直接 `python run.py` 即可。

### 前端启动（开发模式）

```bash
cd frontend
npm install
npm run dev          # http://127.0.0.1:5173 ，自动把 /api 代理到 8000
```

生产构建（产物由后端静态托管）：

```bash
cd frontend
npm run build        # 生成 frontend/dist
```

---

## 6. 可选环境变量

### Agent / 压缩

| 变量 | 作用 | 默认 |
|---|---|---|
| `APP_JWT_SECRET` | JWT 签名密钥，生产环境务必设置 | 开发用内置值 |
| `APP_ACCESS_TTL_SECONDS` | access token 有效期（秒） | 86400（1 天） |
| `APP_REFRESH_TTL_SECONDS` | refresh cookie 有效期（秒） | 2592000（30 天） |
| `AGENT_SOFT_LIMIT` | 覆盖自动压缩的 token 软线阈值 | 取用户模型的 `auto_compact_tokens`，否则 90% 上下文窗口 |
| `AGENT_HARD_LIMIT` | 覆盖硬线裁剪的 token 阈值 | 取用户模型的 `hard_limit_tokens`，否则 95% 上下文窗口 |
| `AGENT_MAX_TURNS` | 主 Agent 单轮最大模型轮数；兼容旧名 `AGENT_RECURSION_LIMIT` | 300 |
| `AGENT_WALL_CLOCK_S` | 整轮墙钟上限（秒）；0 = 不限。`max_turns` 只限次数，这一条限时间 | 0 |
| `HLZ_LLM_MAX_CONNECTIONS` | 模型客户端连接池上限（主轮与所有子代理共享） | 32 |
| `HLZ_LLM_MAX_RETRIES` | 单次模型调用的重试次数（clamp 到 0–8） | 4 |

### 子代理 / Team

| 变量 | 作用 | 默认 |
|---|---|---|
| `SUBAGENT_MAX_DEPTH` | 子代理最大派生深度 | 2 |
| `SUBAGENT_MAX_ACTIVE` | 每个会话同时存活的子代理上限 | 4 |
| `SUBAGENT_TIMEOUT_S` | 单个子代理墙钟超时（秒），超时置 stopped | 300 |
| `SUBAGENT_MAX_TURNS` | 单个子代理模型轮数上限；可被 `spawn_agent` / `spawn_teammate` 的 `max_turns` 覆盖 | 300 |
| `TEAM_DRAIN_TIMEOUT_S` | lead 已结束但 teammate 仍运行时，SSE 等待完成的上限（秒） | 300 |
| `GLOBAL_MAX_ACTIVE_TEAMS` | 全进程活跃 Team 上限 | 16 |
| `USER_MAX_ACTIVE_TEAMS` | 每个用户活跃 Team 上限（会话级已保证一会话一 Team，这一级只管跨用户公平性，故不为 1） | 4 |
| `SESSION_MAX_ACTIVE_TEAMS` | 每个会话活跃 Team 上限 | 1 |
| `TEAM_MAX_ACTIVE_SUBAGENTS` | 每个 Team 同时活跃的子代理/teammate 上限 | 4 |
| `USER_MAX_ACTIVE_SUBAGENTS` | 每个用户活跃子代理上限 | 8 |
| `GLOBAL_MAX_ACTIVE_SUBAGENTS` | 全进程活跃子代理上限 | 64 |

### 资源闸门与 MCP

| 变量 | 作用 | 默认 |
|---|---|---|
| `USER_MAX_CODE_RUNS` | 每个用户并发 `code_run` 上限 | 2 |
| `GLOBAL_MAX_CODE_RUNS` | 全进程并发 `code_run` 上限 | 8 |
| `USER_MAX_MCP_CALLS` | 每个用户并发 MCP 调用上限 | 2 |
| `GLOBAL_MAX_MCP_CALLS` | 全进程并发 MCP 调用上限 | 32 |
| `MCP_CONNECT_TIMEOUT` | 单个用户 MCP server 连接超时（秒），超时/失败自动跳过 | 12 |

### 文件下载

| 变量 | 作用 | 默认 |
|---|---|---|
| `WORKSPACE_MAX_DOWNLOAD_MB` | 单文件下载大小上限（MB） | 200 |
| `WORKSPACE_MAX_ZIP_FILES` | 一次打包的文件数量上限 | 100 |
| `WORKSPACE_MAX_ZIP_MB` | 一次打包的总大小上限（MB） | 500 |

### 韧性（API 中断自愈）

| 变量 | 作用 | 默认 |
|---|---|---|
| `HLZ_LLM_MAX_RETRIES` | 单次模型调用的重试次数，**硬上限 8** | 4 |
| `HLZ_AGENT_LLM_RETRIES` | 模型调用彻底失败后，agent 循环的续跑次数，**上限 3** | 2 |
| `HLZ_LEAD_TEAM_GRACE_TURNS` | lead 想收工但队友仍在跑时，允许续跑的轮数，**上限 10** | 3 |

> 重试预算取值的优先级是「用户模型配置 > 环境变量 > 默认」。
> 用户模型配置表（`user_llm_configs`）没有这个字段，所以通常走环境变量或默认。

### 日志与可观测性

| 变量 | 作用 | 默认 |
|---|---|---|
| `HLZ_LOG_LEVEL` | 应用日志级别；显式 `OFF` / `0` 可完全关掉 | `INFO` |
| `HLZ_TEAM_LOG` | 团队事件 JSONL；`0` 关闭 | 开启 |
| `HLZ_APP_LOG` | 应用日志文件名（相对 `backend/data/logs/`） | `app.log` |
| `HLZ_APP_LOG_ON` | 应用日志落文件开关 | 开启 |

示例：`AGENT_SOFT_LIMIT=200 python run.py`（可快速演示自动压缩）。

排查协作问题时：

```bash
python tools/team_timeline.py --list                  # 日志里出现过的团队
python tools/team_timeline.py --team team_xxxxxxxx    # 按时间线复盘一次协作
python tools/team_timeline.py --message tmsg_xxxx     # 追一条消息的完整生命周期
```

时间线末尾会自动诊断：消息滞留、投递失败、带错误结束的 agent、
以及「API 中断自愈」情况（重试几次、是否放弃、lead 因等队友续跑几次）。

---

## 7. Agent Team（协作）

每轮生成开始时，服务端会为该 `user_id + session_id + generation_id` 创建一个**隔离的 Team**
（`team_id` 由服务端生成，模型无法伪造）。主 Agent 即 **lead**；它可以把任务拆成任务板条目，
再派 **teammate** 执行。

**lead 的 8 个编排工具**：

```text
team_status  task_create  task_get  task_list  task_update
send_message  list_messages  spawn_teammate
```

**teammate 只有 5 个**（管理类工具在 registry 里根本不存在，不是靠 prompt 约束）：

```text
task_get  task_list  task_update  send_message  list_messages
```

关键语义：

- **持久任务板**：`pending / in_progress / completed / cancelled`；支持 `blocked_by` 依赖。
- **CAS 所有权**：`task_update` 必须带读取时的 `expected_version`，防止并发覆盖；`claim / complete / reclaim` 三种动作。
- **团队消息**：`note / result / request`；teammate 默认只读发给自己的消息。
- **失败回收**：`spawn_teammate` 失败会回收任务；Team 创建失败只发 `team_unavailable`，普通子代理链路仍可用。
- **跨轮恢复**：新一轮可恢复上一轮任务板快照（`team_resumed`），不会再因为 lead 轮结束而丢失任务。
- **前端**：右侧 `TeamPanel` 展示任务 / 消息 / 动态；SSE 增量更新，刷新后走 REST 恢复，展开偏好按用户持久化。
- **停止**：主 Agent 停止 → teammate / 子代理 / `code_run` 级联停止；陈旧 generation 租约自动收敛。

**韧性（一个 agent 出问题不拖垮团队）**：

- **lead 收工闸**：模型想结束这一轮、但队友还在跑时，平台把它拦回去继续等（`HLZ_LEAD_TEAM_GRACE_TURNS`，默认 3 轮上限）。否则 lead 说「等 reader」就收工，reader 的结论永远没人接。
- **失主接管**：owner 处于 `errored / stopped / closed` 终态时，任务可被重新绑定给别的队友；`completed` 不算死。
- **异常自动释放**：队友以 `errored / stopped` 结束时，平台自动把它占的 `in_progress` 任务放回 `pending`，任务板不会烂在死 agent 手里。
- **幂等**：`reclaim` 对「已经释放过」的任务返回成功（以前返回 `conflict`，模型据此以为失败并重建任务）；`create_task` 在同团队内按 subject 去重（同名且未完成时复用）。

**观测**：团队事件默认写 `backend/data/logs/team-<日期>.jsonl`，用
`python tools/team_timeline.py --team <team_id>` 还原时间线。

> Team 与 Subagent 的关系：Subagent 是「派一个通用工作代理」；Team 是在子代理之上加任务板、
> 消息与所有权语义。二者共用同一个 `SubagentHost` 与侧链存储。

---

## 7bis. 中途插话（steering）

**生成过程中可以继续输入。** 按发送不会另起一轮，而是把这句话插进正在执行的那个回合，
模型在下一次模型调用前读到它。

### 用法

直接在输入框里打字回车即可。生成中发送的消息会带「已插入当前任务」角标，
不是新的一轮对话。

### 行为

| 情形 | 结果 |
|---|---|
| 回合正在跑 | 入队，返回 `202 {"queued":true,"queue_depth":n}`；下一次 fold 点注入 |
| 回合刚好结束（竞态） | **降级为普通发送**，不会把消息塞进一个没人消费的队列 |
| 带图片 | P0 不支持，返回 `400` 并说明原因（不静默丢弃） |
| 队列满 / 内容为空 | `429 steer_queue_full` |

注入时的包装文案（避免模型把补充要求当成新问题重答一遍）：

```text
[用户在你执行过程中补充的消息]
<原文>

这是用户在上一轮执行过程中补充的要求，请在本回合内一并处理，
不要当成一个新的对话回合，也不要重新回答最开始的问题。
```

### 注入顺序

用户插话 **优先于** 系统通知：

```
用户插话  →  团队收件箱  →  子代理完成通知
```

用户的补充是本回合要处理的新要求，系统通知是已经发生的事实。

### 实现要点

- 队列**按 generation 存活**（挂在进程内注册表上，不挂 `TurnRuntime`）：最后一条消息可能
  晚于最后一个 fold 点到达，挂在回合对象上会随回合一起消失。
- `drain` 是**原子**的（整体替换列表）：HTTP handler 与 SSE 生成器同时操作，读-清两步会丢消息。
- 单条上限 4000 字符、队列深度 20。
- 注入点复用已有的 `AgentLoop.message_injector`；team 路径与普通对话路径都接（靠 `_steer_aware`
  标记避免重复注入）。

### 协议

```http
POST /api/sessions/{sid}/messages
{ "text": "再补一句西湖的内容", "mode": "queue" }

→ 202 { "queued": true, "generation_id": "g_xxx", "queue_depth": 1 }
```

> 未实现（P1，见方案文档）：`priority:"now"` 立即中止当前回合、队列可编辑、
> Esc 清队列、队列持久化到 DB。

详见 `docs/20261111-用户中途插话steering实施方案.md`。

---

## 7ter. 韧性（API 中断自愈）

上游模型服务不稳定时（502 / 504 / 429 / 连接中断 / 流被截断），平台自动恢复，
而不是让整轮对话死掉。

### 三层兜底

| 层 | 机制 | 解决的问题 |
|---|---|---|
| **L1 调用层**（`llm_client`） | 指数退避 + 抖动、尊重 `Retry-After`、**断流检测**（没有 `[DONE]` 也没有 `finish_reason` 判定为截断）、**降级非流式**重取完整结果、总等待封顶 180s | 旧实现「流里出过 delta 就不再重试」，一次断流就放弃 |
| **L2 循环层**（`agent_loop`） | 模型调用彻底失败后**有限续跑**：注入可执行提示并重发模型调用 | 一次 API 抖动就结束整轮 |
| **L3 团队层** | lead 收工闸 + 失主接管 + 异常自动释放 + 幂等 | 一个 agent 出事拖垮整个团队 |

**L2 的幂等性**：续跑只重发**模型调用**，history 一律不动 —— 工具结果已经在 history 里，
因此工具绝不可能执行第二遍。

**不可重试的错误**：`400 / 401 / 402 / 403 / 404 / 405 / 413 / 422` 一律不重试
（配置、权限、参数问题重试无益，只会白烧额度）。

### 收尾摘要

上游**彻底**不可用时，用户不会只看到一句半截话。平台会追加一段「已完成什么」的收尾：

```text
> **执行中断说明**
> 你的请求「…」在收尾阶段被上游模型服务中断打断。

**中断前已完成**
- ✅ 统计 backend/app 下 .py 文件数（counter）

**尚未完成**
- ⏳ 写一句话总结（未开始）

**执行失败的队友**：worker_c

**接下来可以怎么做**
- 直接重发一次即可：上面的进度都在任务板和上下文里，不会丢。
```

这段**绝不调用模型**（此刻模型正不可用），全部是数据库读取 + 模板拼装。
本轮没有成果时会回退到本会话最近一次有产出的一代 —— 用户说「再总结一下刚才的结果」
时，成果往往属于上一代。

### 用户看到的错误信息

上游报错会被翻译成人话，不再把整段 nginx HTML 甩到界面上：

| 上游 | 用户看到 |
|---|---|
| 502 | 上游模型服务暂时不可用（502 Bad Gateway）。已自动重试并续跑多次仍失败，这不是你的操作问题。稍后重发即可，已完成的工作都在上下文里。 |
| 504 | 上游模型服务响应超时（504 Gateway Timeout）。已自动重试仍失败，通常是上游负载过高。 |
| 429 | 上游模型服务限流（429）。已自动退避重试仍失败，请稍后再试。 |
| 连接中断 | 与模型服务的连接中断，已自动重试仍失败。请检查网络或稍后重发。 |

### 判断「是不是代码缺陷」的准则

上游不稳定期（时不时 502/504）容易把抖动误判成 bug。判据：

**不算缺陷**：总耗时变长、单次 e2e 失败、某 agent turns 偏高、模型输出质量下降。

**算缺陷**：脱离上游可稳定复现（假 client 必红）、结构性不变量被破坏
（owner 指向不存在的 agent / 消息永久 `queued` / `done` 不是末帧）、
**自愈机制本身没生效**（有 502 但日志里没有 `llm_retry`，或重试次数与配置不符）。

排查动作：任何「像 bug」的现象，先查 `team-<date>.jsonl` 里有没有
`llm_retry` / `llm_gave_up`；有就是上游，没有才继续查代码。

详见 `docs/20261111-AgentTeam韧性设计与实施.md`。

---

## 8. 文件产物下载

Agent 的产物（图片、Markdown、CSV、JSON、PDF、Office 文档、压缩包等）都写入
`backend/data/workspaces/<user_id>/`。前端提供三个下载入口：

1. **Markdown 文件胶囊**：正文里的本地文件链接 `[报告](reports/q3.docx)` 渲染成 `FileChip`，点击鉴权下载；
2. **工具步骤行**：`file_write / file_patch` 步骤右侧「下载」，`code_run` 步骤右侧「文件」；
3. **工作区面板**：顶栏「文件」打开 `WorkspacePanel`，面包屑浏览目录、单文件下载、多选打包 zip。

对应端点（全部按当前用户鉴权，越界/软链逃逸返回 403）：

```http
GET  /api/me/workspace-file?path=<相对/绝对路径>[&inline=1]
GET  /api/me/workspace-files?path=&depth=2&limit=500[&include_hidden=1]
POST /api/me/workspace-zip   {"paths": ["reports/q3.docx", "data.csv"]}
```

- 单文件默认 `Content-Disposition: attachment` + `nosniff`；中文名走 `filename*=utf-8''`。
- `inline=1` 仅白名单（`text/plain`、`text/markdown`、`text/csv`、`application/json`、`application/pdf`、`application/yaml`），其余 415。
- 打包上限由 `WORKSPACE_MAX_ZIP_FILES` / `WORKSPACE_MAX_ZIP_MB` 控制；条目名会去 `..` 并自动去重。
- 图片仍走 `/api/me/workspace-image`（可内联渲染），与下载端点职责分开。

Office 生成依赖已固定：`python-docx`、`openpyxl`、`python-pptx`。任意格式下载本身不依赖这些库。

---

## 9. 配置 MCP（标准 mcp.json 粘贴）

MCP 配置采用业界通用的 `mcp.json`（`mcpServers` 对象）格式，与 Claude Desktop 等通用客户端一致。
打开「设置 → 我的 MCP」，把 JSON 粘进文本框，点「保存（按 name 合并）」即可
（解析代码见 `backend/app/mcp_config.py`）。也支持「导入 / 导出」整份配置。

### 示例：sqlbot（业务查表 + 画图）+ exa（联网搜索）

`sqlbot_mcp` 是配套的**只读**数据 MCP（指标字典 / 区域 / 时间序列 / 逃生口 SQL / `chart_spec` 画图规格），
默认以 HTTP 方式独立运行在 `http://127.0.0.1:8765/mcp`。它和本仓库没有代码依赖，只通过 URL 连接。

```bash
cd sqlbot_mcp
pip install -e .        # 只需一次
sqlbot-mcp --transport http --host 127.0.0.1 --port 8765
```

```json
{
  "mcpServers": {
    "sqlbot": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    },
    "exa": {
      "type": "http",
      "url": "https://mcp.exa.ai/mcp"
    },
    "fetch": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "mcp-fetch-server"]
    }
  }
}
```

保存后按 `name` **增量合并**：同名覆盖、新名新增，不会清空已有配置。多个 server 是**聚合**关系：
逐个连接，成功者的工具合并到一起；单个失败只降级跳过它，不影响其它服务器。

字段速查：

| 字段 | 适用 | 说明 |
|---|---|---|
| `type` | 全部 | `http` / `sse` / `stdio`；省略时：有 `command` 视为 `stdio`，否则 `http` |
| `url` | http/sse | 远程 MCP 地址 |
| `headers` | http/sse | 鉴权头，如 `{"Authorization": "Bearer xxx"}` |
| `command` / `args` | stdio | 启动命令与参数 |
| `env` | stdio | 进程环境变量 |

> 若 sqlbot 与后端不在同一台机器，把 `127.0.0.1` 换成实际地址，并确保端口可达。

---

## 10. 目录结构

```text
backend/
  app/
    main.py            # FastAPI 装配 + lifespan（建表/租约恢复）+ 静态托管
    auth.py            # bcrypt + PyJWT + get_current_user
    config.py          # 路径/DB 配置 + 可选 llm.yaml 兜底
    db.py              # SQLite 业务表 DAO（users/sessions/messages/llm/mcp）
    llm.py             # llm.yaml → LLMSettings
    llm_client.py      # 自研 LLM 客户端（httpx，chat/responses 双线流式）
    tools.py           # Tool / ToolRegistry
    builtin_tools.py   # file_read/file_write/file_patch/list_files/code_run/load_skill
    run_guard.py       # code_run 写入边界（Python audit hook，非 OS 沙箱）
    skill_store.py     # Skill 三层发现/解析/渐进披露 + 用户私有 CRUD
    workspace.py       # 每用户 workspace + 路径安全（读写/下载）
    agent_loop.py      # Agent 主循环（tool dispatch + 事件 yield）
    agent.py           # UserRuntime 装配（模型 + MCP + 内置工具 + Skill 提示）
    history_store.py   # session_history 单表读写
    compaction.py      # 软线摘要 / 手动压缩 / 硬线裁剪
    tokens.py          # CJK-aware token 估算
    mcp_client.py      # fastmcp.Client → 自研 Tool
    mcp_config.py      # mcp.json 解析/规范化
    sse.py             # AgentLoop 事件 → SSE 事件 + artifact 检测
    runtime.py         # RuntimeHub + 停止登记 + generation 租约收口
    process_registry.py# 活跃 code_run 进程登记与级联终止
    quota.py           # 全局/用户/团队/代理四级容量闸门
    scope.py           # AgentScope / TeamScope 四级作用域
    subagent/          # 进程内 asyncio 子代理运行时（10 个模块）
    team_store.py      # Team 事实表 + 任务板 + 消息 + 事件 + generation 租约
    team_host.py       # TeamHost：lead/teammate 能力边界与生命周期租约
    team_tools.py      # lead 8 工具 / teammate 5 工具
    team_resume.py     # 跨轮任务板快照恢复
    schemas.py         # Pydantic 模型
    routers/
      auth.py          # /api/auth
      me.py            # /api/me LLM 配置 + MCP 服务器（含导入导出）
      skills.py        # /api/me/skills + /api/skills + 公用技能导入
      media.py         # /api/me/workspace-image
      workspace_files.py # /api/me/workspace-file / -files / -zip
      sessions.py      # /api/sessions（含 context usage）
      chat.py          # /api/sessions/{id}/messages(SSE) /stop /compact、/api/mcp/tools
      team.py          # /api/sessions/{id}/team（任务/事件/消息只读快照）
  mcp_server/server.py # FastMCP 示例工具（time/calc/echo/kv）
  skills_builtin/      # 只读内置 Skill（如 data-analysis）
  skills_shared/       # 公用只读 Skill（如 skill-creator / technical-report-writing）
  scripts/             # 一次性迁移脚本
  tests/               # 154 项单元测试（内核/Skill/子代理/Team/文件/停止）
  run.py               # 启动入口
frontend/
  src/
    App.tsx / api.ts / types.ts / files.ts
    components/
      Sidebar.tsx MessageList.tsx Composer.tsx LoginPage.tsx SettingsModal.tsx
      SubagentCard.tsx TeamPanel.tsx WorkspacePanel.tsx FileChip.tsx
      Markdown.tsx MarkdownImage.tsx ChartView.tsx
    styles/index.css   # Design System v2（暖纸 + 陶土）
```

---

## 11. API 概览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查（模型、MCP 工具数） |
| POST | `/api/auth/register` / `/api/auth/login` | 注册 / 登录（返回 access token + refresh cookie） |
| POST | `/api/auth/logout` / `/api/auth/refresh` | 登出 / 刷新 token |
| GET | `/api/auth/me` | 当前用户 |
| GET/POST/PATCH/DELETE | `/api/me/llm-configs` | 用户级 LLM 配置（api_key 打码，仅本人） |
| GET/POST/PATCH/DELETE | `/api/me/mcp-servers` | 用户级 MCP 服务器 |
| POST/GET | `/api/me/mcp-servers/import` / `export` | 整份 `mcp.json` 导入 / 导出 |
| GET | `/api/me/mcp-effective` | 当前生效工具列表 |
| GET | `/api/me/llm-models` | 可选模型列表（会话模型下拉） |
| GET | `/api/sessions` | 会话列表（当前用户） |
| POST | `/api/sessions` | 新建会话（可指定 `model_id`） |
| GET | `/api/sessions/{id}` | 会话详情 + 历史消息（resume 回放） |
| GET | `/api/sessions/{id}/context` | 上下文占用（token / 压缩阈值） |
| PATCH | `/api/sessions/{id}` | 改标题 / 置顶 / 切换模型 |
| DELETE | `/api/sessions/{id}` | 删除会话（连带删历史、子代理、Team 数据） |
| GET | `/api/sessions/{id}/messages` | 历史消息 |
| POST | `/api/sessions/{id}/messages` | 发送消息（SSE 流式）。`mode:"queue"` 时改为插进正在跑的回合，返回 `202 {"queued":true,"queue_depth":n}` |
| POST | `/api/sessions/{id}/stop` | 停止生成（返回已持久化消息快照） |
| POST | `/api/sessions/{id}/compact` | 手动压缩上下文 |
| GET | `/api/sessions/{id}/team` | 团队快照（team / tasks / events / messages） |
| GET | `/api/sessions/{id}/team/tasks` | 任务列表 |
| GET | `/api/sessions/{id}/team/events?since_seq=` | 增量团队事件 |
| GET | `/api/sessions/{id}/team/messages` | 团队消息 |
| GET | `/api/mcp/tools` | 当前可用工具清单（内置 + MCP） |
| GET | `/api/skills` / `/api/skills/shared` | 内置 / 公用 Skill（只读） |
| GET/POST | `/api/me/skills` | 列出可见 Skill / 新建私有 Skill |
| GET/PATCH/DELETE | `/api/me/skills/{name}` | 私有 Skill 详情 / 修改 / 删除 |
| POST | `/api/me/skills/import` / `/api/skills/shared/import` | zip 技能包导入（私有 / 公用） |
| GET | `/api/me/workspace-image?path=` | Markdown 图片鉴权读取（可内联） |
| GET | `/api/me/workspace-file?path=` | 任意格式单文件下载 |
| GET | `/api/me/workspace-files?path=` | 工作区目录列表 |
| POST | `/api/me/workspace-zip` | 多选文件打包下载 |

---

## 12. SSE 事件

`POST /api/sessions/{id}/messages` 返回 `text/event-stream`。前端契约的单一 TypeScript 定义是
`frontend/src/types.ts` 的 `StreamEvent`。

| SSE 事件 | 关键字段 | 用途 |
|---|---|---|
| `message_start` | `message_id` | 绑定本轮 assistant 展示消息 |
| `text_delta` | `text` | 追加正文 |
| `thinking_delta` | `text` | 追加 reasoning |
| `tool_start` | `id, name, input` | 插入工具步骤 |
| `tool_result` | `id, name, content, is_error` | 回填工具结果 |
| `chart` | `spec` | 自描述 artifact 渲染（ECharts） |
| `compaction` | `summary_tokens, kept_messages, trigger` | 自动/手动压缩提示 |
| `healed` | `repaired` | 悬空 tool_call 已修复 |
| `subagent_start` | `agent_id, name, role, task` | 子代理卡片开始 |
| `subagent_event` | `agent_id, event` | 子代理内部增量（文本/思考/工具） |
| `subagent_end` | `agent_id, status, result, turns, error` | 子代理结束 |
| `team_event` | `event` | Team 任务/消息/事件（`team_start / task_created / task_updated / team_message / team_end / team_resumed / team_unavailable`） |
| `message_injected` | `text` | 平台注入到上下文的内容（团队消息 / 子代理通知 / **用户中途插话**），前端渲染成可折叠块并持久化 |
| `llm_retry` | `attempt, limit, error` | 模型调用失败后循环层续跑（韧性 L2） |
| `team_wait` | `reason, grace_left` | lead 想收工但队友还在跑，被拦回去继续等（韧性 L3） |
| `done` | `message_id, status, model` | 终态（`complete / stopped / error`） |
| `error` | `error, hint?` | 错误提示与错误状态 |

> `tool_input` 仍保留在前端类型里做兼容，后端当前不单独发送它（`tool_start` 已带完整 input）。

---

## 13. 测试

```bash
cd backend
python -m pytest tests -q          # 2026-11-06 基线：154 passed
```

覆盖：内核主循环 / 历史与压缩 / Skill 与内置工具 / 子代理 store-host-chat / Team store-host-tools-api /
文件下载端点 / 停止与恢复 / 配额。

前端构建校验：

```bash
cd frontend
npm run build
```

真实 LLM E2E 与 Playwright UI 验证记录见 `docs/20261106-任意格式文件下载实施记录.md`、
`docs/20261106-Web自测Bug修复记录.md`、`docs/20261106-changelog-AgentTeamP3.md`。

---

## 14. 测试账号（开发数据库）

`backend/data/app.db` 不入库；干净 clone 后没有任何账号。开发数据库当前包含：

| 用户名 | 密码 | 说明 |
|---|---|---|
| `test` | `12345678` | 已配置默认模型与 `exa` / `sqlbot` MCP（以实际数据库为准） |

```bash
# 快速验证登录（返回 access_token）
curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"identifier":"test","password":"12345678"}'
```

---

## 15. 说明与边界（MVP）

- **多用户**：邮箱或用户名 + 密码登录，数据按 `user_id` 隔离；越权访问会话返回 404。未做邮箱验证 / 找回密码 / OAuth。
- **注册校验**：邮箱、用户名均唯一；用户名只允许字母、数字、`_`、`.`、`-`，长度 2-32。
- **单副本**：`RuntimeHub`、停止信号、Team 租约、容量闸门都是**进程内**状态；增加 uvicorn worker 不会共享，
  进程重启会中断运行中的生成（已完成结果保留在 SQLite）。
- **停止语义**：协作停止旗 + `task.cancel` + 子代理/`code_run` 级联三层；停止后已生成内容会落库并在 UI 保留。
- **Team**：每轮生成创建一个隔离 Team；Team 是可选协作面，创建失败只发 `team_unavailable`，不影响普通对话。
- **上下文压缩**：软线自动摘要（CJK-aware）+ 手动压缩 + 硬线裁剪；压缩/裁剪/停止都不会产生孤立 tool 消息。
- **安全边界**：`code_run` 是受控子进程（cwd 限制 / 超时 / 输出截断 / Python audit hook 写入守卫），
  **不是 OS 级沙箱**；文件读写与下载限制在个人 workspace；MCP 可连接任意用户配置的远程服务。
- **密钥存储**：用户 APIKey 存在 SQLite 明文列（MVP）；仓库不携带任何 key，`llm.yaml` 与 `backend/data/` 不入库。
- **依赖边界**：不引入 LangChain / LangGraph；MCP 由 `fastmcp.Client` 直接接入并封装为自研 `Tool`。
