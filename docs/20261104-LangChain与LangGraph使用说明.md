# LangChain / LangGraph 使用说明（甲方约定 · 持续更新）

> **本文件的用途**：本项目是甲方要求**必须基于 LangChain + LangGraph 构建**的 Web Agent 服务。
> 本文说明「本项目确实是用这两个库做的」，并逐模块列出**哪些代码用了哪些 LangChain / LangGraph 能力**，
> 便于甲方核查、也便于后续维护者不偏离技术栈。
>
> **保持更新是硬性要求**：任何新增/修改涉及 `langchain*` / `langgraph*` 用法的代码，
> **必须在同一次改动里更新本文（§4 模块映射表 + §6 变更记录）**，否则视为未完成。
>
> 创建日期：2026-11-04（与当前代码同一提交基线）

---

## 1. 结论先行

- 本项目的 **Agent 运行时、模型接入、工具调用循环、会话持久化 / resume、上下文压缩、流式事件** 全部由
  **LangChain + LangGraph** 提供，**不是**手写循环或用其它 Agent 框架。
- LangChain + LangGraph 在本项目中承担的角色，等价于 `GenericAgent` 里那套「Agent 服务层」：
  我们不再自己写 ReAct 循环 / 状态机 / checkpoint，而是用库的积木拼装。
- 少量「胶水层」（MCP 封装、SSE 归一化、多用户 RuntimeHub）是自研的**薄适配**，但底座能力都来自这两个库（见 §5）。

---

## 2. 版本基线

| 包 | 本项目实测版本 | 用途 |
|---|---|---|
| `langchain` | 1.4.8 | Agent 装配（`create_agent`） |
| `langchain-core` | 1.7.4 | 消息类型（`HumanMessage`/`RemoveMessage`）、工具抽象（`StructuredTool`） |
| `langchain-openai` | 1.7.3 | `ChatOpenAI`（含 `openai_chat` / `openai_responses` 两种线） |
| `langgraph` | 1.2.19 | 图运行时、`astream_events` 流式、状态读写（`aget_state`/`aupdate_state`） |
| `langgraph-checkpoint-sqlite` | 3.1.3 | `AsyncSqliteSaver`（把会话状态存进 SQLite，支撑持久化 / resume） |

> 依赖声明见 `backend/requirements.txt`（**已用 `==` 精确锁定**，与上表一致）；前端依赖见 `frontend/package.json` + `frontend/package-lock.json`（同样锁定精确版本）。
> 版本以 `pip show` 实测为准，升级库后请同步更新上表与 `requirements.txt`（§7 有校验命令）。

---

## 3. 这两个库在本项目里各自负责什么

```
浏览器(React) ──HTTP/SSE──▶ FastAPI 路由
                               │
                               ▼
                     RuntimeHub（自研，按用户+模型懒构建）
                               │  使用 ↓
        ┌──────────────────────┼───────────────────────┐
        ▼                      ▼                       ▼
  langchain.create_agent   langgraph.AsyncSqliteSaver  langchain_openai.ChatOpenAI
  （Agent 装配 + 中间件）   （checkpoint / resume）      （模型接入）
        │                                              │
        └────────── langgraph.astream_events ──────────┘
                         （流式事件 → 归一化 SSE）
```

- **LangChain 负责「怎么搭 Agent / 怎么接模型与工具」**：`create_agent` 装配 Agent、`ChatOpenAI` 接各家模型、
  `StructuredTool` 把 MCP 工具变成 Agent 可调用的工具。
- **上下文压缩的阈值判断与摘要触发为自研**（`compaction.py`，基于 LangGraph 状态 API）；
  不再依赖 `SummarizationMiddleware`（原因见 §6 变更记录）。
- **LangGraph 负责「运行态与状态」**：图执行与流式事件（`astream_events`）、会话状态持久化与恢复
  （`AsyncSqliteSaver`）、直接读写会话状态（`aget_state` / `aupdate_state`）。

---

## 4. 模块 → 库能力 映射表（核查用）

> 路径均相对仓库根目录。凡是标「LangChain / LangGraph」的行，即为使用这两个库的代码位点。

| # | 文件 | 用到的库 / API | 在本项目的作用 |
|---|---|---|---|
| 1 | `backend/app/agent.py` | LangChain：`langchain.agents.create_agent`<br>LangGraph：`langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` | **Agent 装配核心**。`create_agent(model, tools, system_prompt, checkpointer)` 组装 Agent；checkpointer 用 `AsyncSqliteSaver` 落 SQLite。自动压缩改由 `compaction.enforce_soft_limit` 在发送前按 token 阈值触发 |
| 2 | `backend/app/llm.py` | LangChain：`langchain_openai.ChatOpenAI` | **模型接入层**。把 `llm.yaml` / 用户配置转成 `ChatOpenAI`；`use_responses_api=True` 走 responses 线 |
| 3 | `backend/app/mcp_client.py` | LangChain：`langchain_core.tools.StructuredTool`（+ `fastmcp.Client` 取工具） | **工具桥接**。发现 FastMCP 工具后，用 `StructuredTool.from_function` 包成 LangChain 工具，交给 `create_agent` |
| 4 | `backend/app/runtime.py` | LangGraph：`langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` | **运行时/持久化**。全局单例 saver，`thread_id=session_id`，支撑多会话持久化与 resume；RuntimeHub 按 `(user, model)` 懒构建 |
| 5 | `backend/app/sse.py` | LangGraph：`Agent.astream_events(..., version="v2")` | **流式协议适配**。消费 LangGraph 事件流，归一化为 `text_delta/thinking_delta/tool_start/tool_result/...` 的 SSE 帧 |
| 6 | `backend/app/compaction.py` | LangChain：`langchain_core.messages.HumanMessage`/`RemoveMessage`/`ToolMessage`、`model.ainvoke`<br>LangGraph：`REMOVE_ALL_MESSAGES`、`agent.aget_state` / `agent.aupdate_state` | **压缩核心**。软线自动（`enforce_soft_limit`，按 token 阈值）+ 手动 `/compact` + 硬线裁剪：读状态→摘要→用 `RemoveMessage(REMOVE_ALL_MESSAGES)` + 新消息数组写回 checkpoint |
| 7 | `backend/app/tokens.py` | 纯自研（CJK 感知 token 估算） | **压缩配套**。`message_token_counter` 供自研软线/硬线阈值判定使用 |
| 8 | `backend/app/routers/chat.py` | 间接：调用上面的 `agent`/`sse`；LangGraph 状态经 `runtime` 的 saver | **聊天入口**。SSE 生成、停止、手动压缩接口；发消息前先调 `enforce_soft_limit`（按 token 阈值自动压缩）再调硬线裁剪 |
| 9 | `backend/app/routers/sessions.py` | 间接：`hub.saver.adelete_thread()` 清理 LangGraph checkpoint | **会话 CRUD**。删除会话时同步删除 LangGraph thread checkpoints |
| 10 | `backend/app/routers/me.py` | 间接：配置变更后 `hub.invalidate()` 驱逐并重建 Agent runtime | **用户配置**。LLM/MCP 配置变更触发 Agent 重建 |

### 4.1 LangChain / LangGraph API 使用清单（精确到符号）

- **LangChain**
  - `langchain.agents.create_agent` — 装配 Agent（`agent.py`）
  - `langchain_openai.ChatOpenAI` — 模型客户端（`llm.py`）
  - `langchain_core.tools.StructuredTool` / `StructuredTool.from_function` — 工具封装（`mcp_client.py`）
  - `langchain_core.messages.{HumanMessage, RemoveMessage}` — 消息构造（`compaction.py`、`sse.py`）
- **LangGraph**
  - `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver`（+ `from_conn_string`）— checkpoint（`agent.py`、`runtime.py`）
  - `Agent.astream_events(..., version="v2")` — 流式事件（`sse.py`）
  - `Agent.aget_state` / `Agent.aupdate_state` — 会话状态读写（`compaction.py`）
  - `langgraph.graph.message.REMOVE_ALL_MESSAGES` — 整体替换上下文（`compaction.py`）
  - `AsyncSqliteSaver.adelete_thread` — 删除会话 thread（`routers/sessions.py`）

---

## 5. 自研部分 vs 库能力（边界说明）

为便于甲方判断「哪些是库给的、哪些是我们写的」，明确划分：

| 能力 | 由谁提供 | 说明 |
|---|---|---|
| Agent 工具调用循环（ReAct 式） | **LangChain** `create_agent` | 不手写多步思考/工具循环 |
| 模型接入（chat/responses 两协议） | **LangChain** `ChatOpenAI` | 不手写 HTTP 模型协议 |
| 自动上下文摘要（触发判定） | 自研（基于 **LangGraph** `aget_state`/`aupdate_state`） | 按 token 软线阈值判定后调用 `model.ainvoke` 生成摘要；不再用 `SummarizationMiddleware`（见 §6） |
| 会话持久化 / resume | **LangGraph** `AsyncSqliteSaver` | 不自研 checkpoint 存储 |
| 流式事件 | **LangGraph** `astream_events` | 不手写 token 流循环 |
| 会话状态读写（压缩用） | **LangGraph** `aget_state`/`aupdate_state` | 不自研状态容器 |
| MCP 工具 → Agent 工具 | 自研薄封装 + **LangChain** `StructuredTool` | fastmcp 取工具，LangChain 承载为工具类型 |
| SSE 归一化（事件→前端协议） | 自研（消费 **LangGraph** 事件） | 仅做协议映射，不改 Agent 行为 |
| 多用户 RuntimeHub / 鉴权 / DB 表 | 自研 | 多用户隔离、配额、JWT 等属于平台层 |
| 硬线裁剪兜底（防超窗） | 自研（基于 **LangGraph** 状态 API） | 软线摘要之外的安全网（不调 LLM），参考 GenericAgent |

> 结论：**核心 Agent 能力 100% 来自 LangChain / LangGraph**；自研集中在平台层（多用户、鉴权、协议适配）与安全兜底。

---

## 6. 变更记录（每次改动 LangChain/LangGraph 用法都要追加）

| 日期 | 变更 | 涉及模块 | 提交 |
|---|---|---|---|
| 2026-11-04 | 建立本文档；梳理全量 `langchain*`/`langgraph*` 使用位点 | 见 §4 | 初始 |
| 2026-11-04 | 上下文压缩三级机制：新增硬线裁剪（`aget_state`/`aupdate_state`/`REMOVE_ALL_MESSAGES`），自动压缩事件去重 | `compaction.py`、`sse.py`、`agent.py` | commit `cd93dc9` |
| 2026-11-04 | 修复思考内容不显示：`build_chat_model()` 透传 `reasoning_effort`；`extract_reasoning()` 兼容 Responses 线 `{'type':'reasoning','summary':[{'text':...}]}` 形态；前端默认展开显示「思考过程」 | `llm.py`、`sse.py`（复用）、`MessageList.tsx`、`index.css` | 本次提交 |
| 2026-11-04 | 自动压缩改为「发送前按 token 软线阈值」自研判定（对齐 GenericAgent `should_auto_compact_agent`）：新增 `compaction.enforce_soft_limit`；移除 `SummarizationMiddleware` 及其在 `sse.py` 的 `on_chain_end` 误报（手动压缩后每轮被误判为 auto 压缩） | `compaction.py`、`agent.py`、`sse.py`、`routers/chat.py` | 本次提交 |

---

## 7. 如何核查（一条命令定位全部用法）

在仓库根目录执行（Git Bash）：

```bash
# 1) 列出所有使用 LangChain / LangGraph 的代码位点
grep -rn "langchain\|langgraph" backend --include=*.py

# 2) 确认运行时实际版本
python - <<'PY'
import importlib.metadata as m
for n in ["langchain","langchain-core","langchain-openai","langgraph","langgraph-checkpoint-sqlite"]:
    print(n, m.version(n))
PY
```

预期：`backend/app/` 下 `agent.py / llm.py / mcp_client.py / runtime.py / sse.py / compaction.py / tokens.py`
均出现相应 import（与 §4 表一致）。

---

## 8. 维护约定（重要）

1. **新增 Agent / 模型 / 工具 / 压缩 / 持久化相关功能，必须先确认用 LangChain/LangGraph 原生能力实现**；
   确需自研兜底（如硬线裁剪），须在本文 §5 明确标注「自研」及理由。
2. **任何涉及这两个库的代码改动，同一次提交内更新 §4、§6**（改了什么模块、用了哪些 API、对应提交号）。
3. 升级这两个库的版本时，更新 §2 版本表，并在 §6 记一条。
4. 本文与 `docs/20261104-实施方案与进度.md`、`AGENTS.md` 交叉引用，保持一致。
