# 多用户 Web Agent（GenericAgent 内核 · 自研 Agent 运行时）

这是一个**支持多用户的 Web Agent 服务**：用户注册/登录后，各自拥有**互相隔离**的会话、消息、
APIKey（模型端点）与 MCP 配置，互不可见。

技术栈：**FastAPI（自研 Agent 内核）+ React**，**不依赖 LangChain / LangGraph**。
能力包含：登录/注册 + 会话按用户隔离 + 会话级模型切换 + 用户级 APIKey / MCP 配置 +
Web 前端 + Agent 后端（自研 LLM 客户端 + 工具调用循环）+ SQLite 会话持久化 / resume / 删除 +
上下文压缩 + FastMCP（标准 mcp.json）工具接入 + **Skill 模块**（SKILL.md 渐进披露） + **内置本地工具**（file_read / file_write / file_patch / list_files / code_run / load_skill） + **子代理（Subagent）**（进程内 asyncio 运行时：spawn / wait / read / list / close 五个编排工具；子代理继承全部业务工具与 MCP，但不能再派生子代理；事件合并进同一 SSE，前端可折叠卡片）；Markdown 图片 `![](...)` 支持 workspace 本地路径（带鉴权读取）与远程 URL。

> 界面示意：同一个会话内 Agent 先查数（工具卡片）再画图（ECharts 渲染，可「下载 PNG」），
> 多轮对话与图表一起按会话持久化；左侧会话列表按用户隔离。

> 本项目由 `jiakuan_langchain` 快照演化而来，现已去除 LangChain / LangGraph，改为借鉴
> `D:\git_codes\GenericAgent` 设计、在本项目自研实现 Agent 内核。
> 本 README 只讲怎么跑。

---

## 技术栈

- 后端：FastAPI + **自研 Agent 内核**
  - `llm_client.py`：httpx 直连模型（`openai_chat` / `openai_responses` 两线 + 流式 + reasoning）
  - `agent_loop.py`：显式工具调用循环（模型→工具→回填→再模型），事件流式 `yield`
  - `tools.py`：自研工具抽象（名字/描述/JSON Schema/调用），替代 `StructuredTool`
  - `history_store.py`：会话历史单一权威存储（SQLite）
  - 上下文自动压缩：发送前按 token 阈值判断（对齐 GenericAgent）
  - `builtin_tools.py`：内置工具 file_read / file_write / file_patch / list_files / code_run / load_skill
  - `skill_store.py`：文件系统 Skill（SKILL.md）解析 / 渐进披露 / 用户私有 CRUD
  - `workspace.py`：每用户 workspace + 路径安全（读写均限定在个人目录内）
  - `subagent/`：进程内 asyncio 子代理运行时（context / store / events / capabilities /
    registry / runtime / host / tools / prompts / session），不引入 langchain/langgraph、不 import GA
- 认证：邮箱+密码（bcrypt）+ JWT access token + HttpOnly refresh cookie（旋转）
- 多用户：会话/消息/AI 配置全部按 `user_id` 隔离；每用户可配置自己的模型端点与 MCP 服务器
- 会话级模型：每个会话可在顶栏「模型」下拉切换（`sessions.model_id`），支持同一用户多模型（如 deepseek / glm）
- 模型：**每个用户各自配置**（`base_url` / `api_key` / `model` / `wire_api`），互不可见；仓库不携带任何 key
- 工具：FastMCP（独立 server 模块，后端以 `fastmcp.Client` 接入并封装为自研工具）
- 存储：SQLite（`backend/data/app.db`，业务表 + 会话历史同库）
- 前端：React 18 + Vite + TypeScript，SSE 流式渲染

### 默认模型的选择规则

1. 用户在「设置 → 我的模型」里勾选的那条（`is_default=1`）就是默认；**这是唯一由用户决定的口径**。
2. 若从未勾选过默认（历史数据/导入的配置），系统回退到**最早创建**的那条可用配置，
   也就是下拉列表里的第一项（不是最近编辑的那条），并自动把它标记为默认，让 UI 显示「默认」标签。
3. 新建会话若没指定模型，就用上面这条默认；会话一旦指定了 `model_id`，就固定用它（即使默认后来变了）。
4. 配置列表按「默认优先 + 创建时间」排序，因此下拉顺序稳定，不会因为编辑某个模型而漂移。

## 环境要求

- Python 3.12（命令用 `python`）
- Node.js（已测 v24）
- 依赖见 `backend/requirements.txt`（已固定版本）与 `frontend/package.json`

> **数据库与 `llm.yaml` 均为本机运行时数据，不入库。**
> - 干净 clone 后首次启动会自动创建空的 `backend/data/app.db`（无用户/会话）。
> - 本机开发库里已内置测试账号：用户名 `test` / 密码 `12345678`，且已配置默认模型
>   `deepseek-v4.1-flash`，可直接用于真实 LLM E2E（见下文「测试账号」）。
> - 模型 key 由每个用户在页面里自行填写；仓库不携带任何 key。

## 干净 clone 后启动（首次）

```bash
# 1) 后端依赖
cd backend
pip install -r requirements.txt

# 2) 前端构建（产物由后端单端口托管）
cd ../frontend
npm install
npm run build

# 3) 启动后端
cd ../backend
python run.py                        # http://127.0.0.1:8000
```

首次启动会自动创建空的 `backend/data/app.db`（无任何用户/会话）。

1. 浏览器打开 `http://127.0.0.1:8000/`，点「注册」创建账号：邮箱 + 密码，可选填**用户名**（不填则默认取邮箱 @ 前缀）。之后**邮箱或用户名都能登录**。
2. 打开「设置 → 我的模型」，填 `base_url` / `api_key` / `model`，勾选「默认」并保存。
3. 回到会话页，新建会话即可对话。

- 健康检查：`GET http://127.0.0.1:8000/api/health`（`model` 为空表示本机没有可选的 `llm.yaml` 兜底，属正常）
- 若 `frontend/dist` 存在，后端会自动在 `/` 托管前端（单端口访问）。

## 后端启动（日常）

```bash
cd backend
python run.py                        # 启动 http://127.0.0.1:8000
```

## 前端启动（开发模式）

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

## 可选环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `APP_JWT_SECRET` | JWT 签名密钥，生产环境务必设置 | 开发用内置值 |
| `AGENT_SOFT_LIMIT` | 覆盖自动压缩的 token 软线阈值（调参/演示用） | 取该用户模型的 `auto_compact_tokens`，否则 90% 上下文窗口 |
| `AGENT_HARD_LIMIT` | 覆盖硬线裁剪的 token 阈值 | 取该用户模型的 `hard_limit_tokens`，否则 95% 上下文窗口 |
| `AGENT_MAX_TURNS` | 单轮请求内 Agent 循环的最大模型轮数（防死循环） | 48 |
| `MCP_CONNECT_TIMEOUT` | 单个用户 MCP server 的连接超时（秒），超时/失败自动跳过 | 12 |
| `SUBAGENT_MAX_DEPTH` | 子代理最大派生深度（防止无限套娃） | 2 |
| `SUBAGENT_MAX_ACTIVE` | 每个会话同时存活的子代理上限（每会话，非全局） | 4 |
| `SUBAGENT_TIMEOUT_S` | 单个子代理的墙钟超时（秒），超时置 stopped | 300 |
| `SUBAGENT_MAX_TURNS` | 单个子代理的模型轮数上限 | 24 |

示例：`AGENT_SOFT_LIMIT=200 python run.py`（可快速演示自动压缩）。

---

## 配置 MCP（标准 mcp.json 粘贴）

MCP 配置采用**业界通用**的 `mcp.json`（`mcpServers` 对象）格式，与 Claude Desktop / GenericAgent 一致。
打开「设置 → 我的 MCP」，把下面的 JSON 粘进文本框，点「保存（按 name 合并）」即可
（解析代码见 `backend/app/mcp_config.py`）。

### 示例 1：只接 sqlbot_mcp（业务查表 + 画图）

`sqlbot_mcp` 是本项目配套的**只读**数据 MCP（指标字典 / 区域 / 时间序列 / 逃生口 SQL / `chart_spec` 画图规格），
默认以 HTTP 方式独立运行在 `http://127.0.0.1:8765/mcp`。

> 它是**完全独立的另一个项目**（`sqlbot_mcp`），与本仓库没有代码依赖，也不需要放在同一目录。
> 两者只通过上面的 HTTP URL 连接：sqlbot 可以在任意机器/任意目录运行，只要本平台能访问到该地址。
> 本平台不依赖它的任何具体工具名——只要是一个标准 MCP，就能这样接进来。

先把服务起起来：

```bash
cd sqlbot_mcp
pip install -e .        # 只需一次
sqlbot-mcp --transport http --host 127.0.0.1 --port 8765
```

不安装也可以直接跑（源码目录方式）：

```bash
cd sqlbot_mcp
PYTHONPATH=src SQLBOT_DB_TYPE=sqlite python -m sqlbot_mcp.server --transport http --host 127.0.0.1 --port 8765
```

> Windows PowerShell 里设环境变量用 `$env:PYTHONPATH="src"`，或直接用上面的 `pip install -e .` 方式。

然后在「设置 → 我的 MCP」粘贴（这就是 `test` 账号当前实际使用的配置）：

```json
{
  "mcpServers": {
    "sqlbot": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

保存后生效工具应有 12 个：

```text
health  metric_search  metric_list  zone_list  time_options  dimensions
table_schema  metric_series  metric_latest  metric_compare  chart_spec  run_readonly_sql
```

可以这样验证：问「全国家庭宽带用户数近半年趋势，请画折线图」，
Agent 会依次调用 `metric_search → time_options → metric_series → chart_spec`，
页面出现折线图（图随消息持久化，刷新仍在，且只对当前用户可见）。

说明：

- `sqlbot` 是**远程 http** MCP：只给 `url` 即可；`type` 写 `http`，也可省略（有 `url` 就按 http 处理）。
- 若 sqlbot 与后端不在同一台机器，把 `127.0.0.1` 换成实际地址，并确保端口可达。
- 远端若要求鉴权，用 `headers` 传，例如 `{"headers": {"Authorization": "Bearer xxx"}}`。

### 示例 2：stdio（本地进程）

用 `npx` 启动的 MCP：

```json
{
  "mcpServers": {
    "fetch": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "mcp-fetch-server"]
    }
  }
}
```

字段速查：

| 字段 | 适用 | 说明 |
|---|---|---|
| `type` | 全部 | `http` / `sse` / `stdio`；省略时：有 `command` 视为 `stdio`，否则 `http` |
| `url` | http/sse | 远程 MCP 地址（如 `https://mcp.exa.ai/mcp`） |
| `headers` | http/sse | 鉴权头，如 `{"Authorization": "Bearer xxx"}` |
| `command` / `args` | stdio | 启动命令与参数，如 `npx` + `["-y","pkg"]` |
| `env` | stdio | 进程环境变量，如 `{"API_KEY":"xxx"}` |

### 示例 3：多个 MCP 同时接入（sqlbot + 联网搜索 + 抓取）

多个 server 直接并列在 `mcpServers` 里即可；保存时按 `name` **增量合并**，同名覆盖、新名新增，
**不会清空**已有配置。下面是 `test` 账号实际在用的组合（sqlbot 查内部业务数据 + exa 联网搜索）：

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
    }
  }
}
```

再加一个 stdio 抓取服务（需要本机有 Node/npx）：

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

也可以把 GenericAgent 的整份 `mcp.json` 直接粘进来（含 `fetch` / `tavily` / `context7` 等），
保存后按 `name` 合并。多服务器是**聚合**关系：逐个连接，成功者的工具合并到一起；
单个服务器连接失败只降级跳过它，不影响其它服务器。

---

## 目录结构

```
backend/
  app/
    main.py         # FastAPI 装配 + lifespan + 静态托管
    auth.py         # bcrypt + PyJWT + get_current_user 依赖
    config.py       # 路径/DB 配置 + 可选 llm.yaml 兜底解析
    db.py           # SQLite 业务表 DAO（users/sessions/messages/llm_configs/mcp_servers/history）
    llm.py          # llm.yaml → LLMSettings 解析
    llm_client.py   # 自研 LLM 客户端（httpx，chat/responses 两线，流式）
    tools.py        # 自研工具抽象 Tool / ToolRegistry
    builtin_tools.py# 内置工具：file_read/file_write/file_patch/list_files/code_run/load_skill
    skill_store.py  # Skill（SKILL.md）解析/发现/加载 + 用户私有 CRUD
    workspace.py    # 每用户 workspace + 路径安全（支持子代理写根隔离）
    subagent/       # 进程内 asyncio 子代理运行时（编排工具 / 侧链表 / 事件扇出）
    agent_loop.py   # 自研 Agent 主循环（while + tool dispatch + 事件 yield）
    history_store.py# 会话历史单表读写（替代 checkpoint，表 session_history）
    mcp_client.py   # fastmcp.Client -> 自研工具
    sse.py          # AgentLoop 事件 -> SSE 事件
    compaction.py   # 压缩：软线自动（按 token 阈值）+ 手动 /compact + 硬线裁剪兜底
    tokens.py       # CJK-aware token 估计
    runtime.py      # RuntimeHub：按用户懒构建 runtime + 停止登记
    schemas.py      # Pydantic 模型
    routers/
      auth.py       # /api/auth register/login/logout/me/refresh
      me.py         # /api/me 用户级 LLM 配置 + MCP 服务器
      skills.py     # /api/me/skills CRUD + /api/skills 内置列表
      media.py      # /api/me/workspace-image：Markdown 图片鉴权读取
      sessions.py   # /api/sessions CRUD（按 user_id 隔离）
      chat.py       # /api/sessions/{id}/messages(SSE) /stop /compact /mcp/tools
  mcp_server/
    server.py       # FastMCP 示例工具（time/calc/echo/kv）
  skills_builtin/
    data-analysis/SKILL.md  # 内置示例 Skill（只读，所有用户可见）
  scripts/
    migrate_checkpoints.py  # 一次性：旧 langgraph checkpoint -> session_history
  tests/
    test_kernel.py  # 自研内核单元测试
    test_skills_tools.py  # Skill + 内置工具单元测试
    test_subagent_*.py    # 子代理 store/capabilities/host/chat 单元测试
  run.py            # 启动入口
frontend/
  src/
    App.tsx / api.ts / types.ts
    components/Sidebar.tsx MessageList.tsx Composer.tsx LoginPage.tsx SettingsModal.tsx
               SubagentCard.tsx Markdown.tsx MarkdownImage.tsx
```

## API 概览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查（模型、MCP 工具数） |
| POST | `/api/auth/register` / `/api/auth/login` | 注册 / 登录（返回 access token + refresh cookie） |
| POST | `/api/auth/logout` / `/api/auth/refresh` | 登出 / 刷新 token |
| GET | `/api/auth/me` | 当前用户 |
| GET/POST/PATCH/DELETE | `/api/me/llm-configs` | 用户级 LLM 配置（api_key 打码，仅本人） |
| GET/POST/PATCH/DELETE | `/api/me/mcp-servers` | 用户级 MCP 服务器 |
| GET | `/api/me/mcp-effective` | 当前生效工具列表 |
| GET | `/api/me/llm-models` | 可选模型列表（会话模型下拉） |
| GET | `/api/sessions` | 会话列表（当前用户） |
| POST | `/api/sessions` | 新建会话 |
| GET | `/api/sessions/{id}` | 会话详情 + 历史消息（resume 回放） |
| PATCH | `/api/sessions/{id}` | 改标题/置顶 |
| DELETE | `/api/sessions/{id}` | 删除会话（连带删会话历史） |
| GET | `/api/sessions/{id}/messages` | 历史消息 |
| POST | `/api/sessions/{id}/messages` | 发送消息（SSE 流式） |
| POST | `/api/sessions/{id}/stop` | 停止生成 |
| POST | `/api/sessions/{id}/compact` | 手动压缩上下文 |
| GET | `/api/mcp/tools` | 当前可用工具清单（内置 + MCP） |
| GET | `/api/skills` | 内置 Skill（只读） |
| GET | `/api/me/skills` | 当前用户可见 Skill（内置 + 私有） |
| GET/POST | `/api/me/skills` | 列出可见 Skill / 新建私有 Skill |
| GET/PATCH/DELETE | `/api/me/skills/{name}` | 私有 Skill 详情 / 修改 / 删除 |

SSE 事件：`message_start / text_delta / thinking_delta / tool_start / tool_input / tool_result / compaction / done / error`。

## 测试账号（本机开发库）

迁移自旧项目的 `backend/data/app.db` 已包含以下测试账号（仅本机开发用，不入库）：

| 用户名 | 密码 | 说明 |
|---|---|---|
| `test` | `12345678` | 已配置默认模型 `deepseek-v4.1-flash`（`openai_chat`），以及 `exa` / `sqlbot` MCP |

```bash
# 快速验证登录（返回 access_token）
curl -s -X POST http://127.0.0.1:8000/api/auth/login   -H 'content-type: application/json'   -d '{"identifier":"test","password":"12345678"}'
```

## 说明与边界（MVP）

- 多用户：**邮箱或用户名 + 密码**登录，数据按 `user_id` 隔离；越权访问会话返回 404。MVP 未做邮箱验证/找回密码/OAuth。
- 注册校验：邮箱、用户名均唯一（`users.username` 有唯一索引）；重复时分别提示「邮箱已被注册」/「用户名已被占用」。用户名只允许字母、数字、`_`、`.`、`-`，长度 2-32。
- 用户级 APIKey 存于 SQLite（本机 MVP，明文列）；MCP 支持多服务器聚合（逐个连接，成功者工具合并，单个失败降级跳过）。
- 单副本，停止生成用进程内取消（未做跨实例 pub/sub）。
- 上下文压缩：自动软线 = 发送前按 token 阈值判断后用 LLM 摘要（CJK-aware 计数器）+ 手动 `/compact` + 硬线裁剪兜底。
- `llm.yaml` 为**可选**本机兜底配置（未配置用户模型时才会用到），内含真实 key，**不入库**；
  干净 clone 后没有该文件也能正常启动（用户各自配置模型即可）。
- `backend/data/`（SQLite 数据库）不入库，运行时自动创建。
- 本项目不依赖 LangChain / LangGraph；MCP 由 `fastmcp.Client` 直接接入并封装为自研工具。
- 内置工具的文件读写被限制在 `backend/data/workspaces/<user_id>/`；`code_run` 是受控子进程（限 cwd / 超时 / 输出截断，非强沙箱），后续可替换为容器执行。
- Skill 以文件系统为准：内置在 `backend/skills_builtin/`（只读），私有在 `backend/data/skills/<user_id>/`；系统提示只注入名称+描述清单，正文由 `load_skill` 按需加载。
