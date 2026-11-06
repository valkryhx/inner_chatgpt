# 实施文档：去除 LangChain / LangGraph（P0 轻量化基线）

- 日期：2026-11-05
- 状态：**已完成（2026-11-05，见 `docs/20261105-changelog-P0.md`）**
- 配套：`docs/20261105-去LangChain依赖-决策记录.md`（决策）、`docs/20261105-LangChain-LangGraph依赖审计.md`（审计）
- 目标：把后端从 langchain / langgraph 换成 GA 式自研内核，**功能等价**，为后续 P1~P8 高级特性打地基。

---

## 0. 一句话方案

新增 4 个自研模块（`llm_client` / `tools` / `agent_loop` / `history_store`），把现有 7 个接线文件
（`llm` / `agent` / `runtime` / `sse` / `compaction` / `mcp_client` / `routers/sessions`）改为使用它们，
然后删除 5 个 langchain/langgraph 依赖。**SSE 协议、HTTP API、前端契约保持不变。**

## 1. 目标架构

```
浏览器(React) ──HTTP/SSE──▶ FastAPI 路由 (不变)
                              │
                              ▼
                    RuntimeHub（自研，按 用户+模型 懒构建；不变）
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
        LLMClient        AgentLoop         HistoryStore
      (httpx 自研)   (while+dispatch)   (SQLite 单表自研)
              │               │                │
              │        ToolRegistry(自研)        │
              │               │                │
              └────── MCPToolset(fastmcp，保留) ─┘
```

对照旧架构：`ChatOpenAI` → `LLMClient`；`create_agent+astream_events` → `AgentLoop`；
`AsyncSqliteSaver` → `HistoryStore`；`StructuredTool` → `Tool`。

## 2. 目录与文件变化总览

| 文件 | 动作 | 说明 |
|---|---|---|
| `backend/app/llm_client.py` | **新增** | httpx 模型客户端，chat/responses 两线 + 流式 |
| `backend/app/tools.py` | **新增** | `Tool` 数据类 + `ToolRegistry` + 内置 MCP 工具注册 |
| `backend/app/agent_loop.py` | **新增** | 主循环 `AgentLoop.run_stream()` |
| `backend/app/history_store.py` | **新增** | 会话历史读写（SQLite 单表） |
| `backend/app/llm.py` | 重写 | 保留 `LLMSettings` 解析，删除 `ChatOpenAI`；改为构造 `LLMClient` |
| `backend/app/agent.py` | 重写 | `UserRuntime` 用 `LLMClient + ToolRegistry + AgentLoop` |
| `backend/app/runtime.py` | 改 | 删除 `AsyncSqliteSaver`，只保留 Hub 与停止登记表 |
| `backend/app/sse.py` | 改 | `run_agent_stream` 消费 `AgentLoop` 的 yield（事件名不变） |
| `backend/app/compaction.py` | 改 | 操作 `HistoryStore` 的 dict 历史，去掉库消息类型 |
| `backend/app/mcp_client.py` | 改 | `StructuredTool` → 自研 `Tool` |
| `backend/app/routers/sessions.py` | 改 | `saver.adelete_thread` → `history_store.delete` |
| `backend/app/routers/chat.py` | 小改 | 调用链适配（压缩/自愈入口签名不变则无需大改） |
| `backend/app/tokens.py` | 小改 | 对齐 GA 的「真实 usage 校准 + 增量估算」 |
| `backend/requirements.txt` | 改 | 删 5 个库，加 `httpx` |
| `frontend/src/api.ts` | 可选 | `TOKEN_KEY` 更名（不影响功能） |
| `backend/data/app.db` | 迁移 | 新增 `session_history` 表；回填历史 |

> 约 4 个新文件 + 8 个改动文件，预计新增/改动 700~1100 行。

## 3. 统一数据模型（最重要，先定死）

### 3.1 消息 dict（取代库消息类型）

```python
# 用户消息
{"role": "user", "content": "你好"}
# 助手（纯文本）
{"role": "assistant", "content": "..."}
# 助手（带思考）
{"role": "assistant", "content": "...", "reasoning": "..."}
# 助手（发起工具调用）
{"role": "assistant", "content": "", "tool_calls": [
    {"id": "call_abc", "type": "function",
     "function": {"name": "metric_series", "arguments": "{\"month\":\"2026-10\"}"}}
]}
# 工具结果回填
{"role": "tool", "tool_call_id": "call_abc", "name": "metric_series", "content": "..."}
```

- `content` 一律用字符串（多模态后续再扩展成 block list，保持 `_content_blocks()` 归一化位）。
- `reasoning` 是可选的旁路字段（不进模型请求，只用于展示/持久化）。

### 3.2 会话历史

```python
history: list[dict]   # 顺序：system, user, assistant, tool, assistant, ...
```

- 单一权威历史，存于新表 `session_history`。
- `messages` 表继续保留，作为**前端展示用**（blocks/status），二者由 `chat.py` 负责同步（现状已如此）。


---

## 4. 模块详细设计

### 4.1 `llm_client.py`（替换 `ChatOpenAI`）

**职责**：把 `LLMSettings`（含 base_url/api_key/model/wire_api/...）变成可流式调用的客户端。

**接口**：
```python
class LLMError(Exception): ...
@dataclass
class StreamChunk:
    text: str = ""            # 正文增量
    reasoning: str = ""       # 思考增量
    tool_calls: list | None = None   # 仅在最后汇总时给完整 tool_calls
@dataclass
class ChatResult:
    content: str
    reasoning: str
    tool_calls: list[dict]    # 归一化后的完整工具调用
    raw_usage: dict | None

class LLMClient:
    def __init__(self, s: LLMSettings): ...
    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None
                          ) -> AsyncIterator[StreamChunk]: ...
    # 生成器最终 return ChatResult（async generator 用 StopAsyncIteration.value 或回调）
```

**两种 wire（关键实现点）**：

| wire_api | 端点 | 请求体 | 流式增量位置 |
|---|---|---|---|
| `openai_chat` | `POST {base}/chat/completions` | `{model, messages, tools, stream:true, ...}` | `choices[0].delta.content` / `delta.tool_calls[]` |
| `openai_responses` | `POST {base}/responses` | `{model, input, tools, stream:true, ...}` | `response.output_text.delta` / `response.reasoning_summary_text.delta` / `response.output_item.done`（tool call） |

**必须处理的坑（来自现有 `llm.py` 实测注释，不得凭记忆写）**：
1. responses 线流式 `content` 是**块列表**而非字符串；
2. reasoning 在 `summary[].text`（DeepSeek v4.1-flash 实测形态），也可能在 `reasoning_content`；
3. 前 1~2 个 chunk 内容为空（role/reasoning 阶段），要跳过；
4. tool_calls 是**增量拼接**：`delta.tool_calls[i].function.arguments` 逐字符累加，靠 `index` 合并；
5. `max_tokens` / `reasoning_effort` 透传；`use_responses_api=True` 对应 responses 线。

**参考**：GA `genericagent/llm/core.py` 的 `_openai_stream` / `_parse_openai_sse` / `_record_usage`
（1200~1500 行区间附近），三线归一化的做法可直接借鉴思路。

**测试**：单独写 `backend/tests/test_llm_client.py`（或临时脚本），对真实 `llm.yaml` 跑通：
- 发一句普通文本，收到流式正文；
- 发一句需要思考的 prompt，收到 reasoning；
- 带一个 echo 工具，收到结构化 tool_calls。

### 4.2 `tools.py`（替换 `StructuredTool`）

```python
@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict            # JSON Schema
    call: Callable[[dict], Awaitable[str]]

    def to_openai_schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.input_schema or {"type":"object","properties":{}}}}

class ToolRegistry:
    def __init__(self, tools: list[Tool]): ...
    def schemas(self) -> list[dict]: ...
    def get(self, name: str) -> Tool | None: ...
    async def dispatch(self, name: str, args: dict) -> str:
        """永不抛异常：任何错误转成可读文本回喂模型（对齐现有 _call_tool_safe）。"""
```

- `MCPToolset` 改成产出 `list[Tool]`（保留 fastmcp 连接/发现/多服务器合并逻辑；
  只把最后的 `StructuredTool.from_function` 换成 `Tool(...)`）。
- 内置 demo MCP（`backend/mcp_server/server.py`）继续走同一路径，无需特殊处理。

### 4.3 `agent_loop.py`（替换 `create_agent` + `astream_events`）

```python
class AgentLoop:
    def __init__(self, client: LLMClient, registry: ToolRegistry,
                 system_prompt: str, max_turns: int = 48): ...

    async def run_stream(self, history: list[dict], stop_event: asyncio.Event | None = None
                         ) -> AsyncIterator[dict]:
        """yield 归一化事件：text_delta/thinking_delta/tool_start/tool_result/
           (chart 由上层 detect_artifacts 产出)。结束时把最终 history 写回 HistoryStore。"""
```

**主循环伪代码**（与 GA `agent_runner_loop` 同构）：
```
turn = 0
while turn < max_turns and not stopped:
    turn += 1
    result = await collect(client.chat_stream(history, tools=registry.schemas()))
    if result.reasoning: yield thinking_delta（流式时已在 chunk 里 yield）
    if result.content: yield text_delta
    if not result.tool_calls:
        history.append(assistant(content)); break
    history.append(assistant(tool_calls=result.tool_calls))
    for tc in result.tool_calls:
        yield tool_start(id, name, args)
        out = await registry.dispatch(name, args)     # 永不抛
        history.append({"role":"tool", "tool_call_id": tc.id, "name": name, "content": out})
        yield tool_result(id, name, out, is_error)
```

**要点**：
- `recursion_limit` 语义 → `max_turns`（默认 48，环境变量 `AGENT_MAX_TURNS`）；
- 工具结果 budget（截断）逻辑可先简化，后续 P1 再引入 GA 的 `budget_tool_result`；
- **悬空 tool_call 自愈**：`AgentLoop` 在每轮开始前检查 `history` 末尾是否有「assistant.tool_calls
  但缺 tool 结果」，有则补跑或截断（逻辑从现 `agent.py::sanitize_pending_tool_calls` 平移，改成操作 dict）。
- stop：每个 chunk 前检查 `stop_event.is_set()`，中断即结束并保存。

### 4.4 `history_store.py`（替换 `AsyncSqliteSaver`）

```python
CREATE TABLE IF NOT EXISTS session_history (
    session_id TEXT PRIMARY KEY,
    history    TEXT NOT NULL DEFAULT '[]',   -- JSON: list[dict]
    updated_at INTEGER NOT NULL
);
```

```python
class HistoryStore:
    async def get(self, session_id: str) -> list[dict]: ...
    async def set(self, session_id: str, history: list[dict]) -> None: ...
    async def delete(self, session_id: str) -> None: ...
```

- 直接复用 `db.py` 的 `aiosqlite` 连接方式（**不要开长连接**，与现有 DAO 风格一致：每次 connect）。
- 并发：同一会话同一时刻只允许一个生成（现有 `RuntimeHub` 已有 per-(user,model) 锁；会话级再加
  一把 `asyncio.Lock` 即可）。
- **迁移**：把旧 `checkpoints` 表里 `thread_id == session_id` 的最新 checkpoint 还原成 messages，
  写成 `session_history` 的 dict 列表；迁移脚本见 §6。

---

## 5. 接线改造（逐文件）

### 5.1 `llm.py`
- 保留 `LLMSettings` 与 `load_llm_settings()`（读 `llm.yaml`）。
- 删除 `ChatOpenAI` 相关；新增 `build_client(settings) -> LLMClient`。
- `extract_text/extract_reasoning` 迁到 `llm_client.py` 内部（或保留为工具函数）。
- `build_chat_model_from_config(cfg)` → `build_client_from_config(cfg)`，返回 `LLMClient`。

### 5.2 `agent.py`
```python
class UserRuntime:
    async def build(...):
        self.model = await build_user_model(...)       # 返回 BuiltModel(LLMClient,...)
        self.toolset = await build_user_toolset(...)    # 返回工具 list[Tool]
        self.registry = ToolRegistry(self.toolset.tools)
        self.loop = AgentLoop(self.model.client, self.registry, SYSTEM_PROMPT, max_turns=...)
        self.store = get_history_store()
```
- 删除 `create_agent` / `AsyncSqliteSaver` 引用。
- `sanitize_pending_tool_calls` 改为 `AgentLoop` 的方法（操作 dict 历史）。

### 5.3 `runtime.py`
- `RuntimeHub.startup()` 只初始化 `HistoryStore`（代替 `AsyncSqliteSaver.from_conn_string`）。
- `shutdown()` 不再 `__aexit__` saver。
- 其余（懒构建、锁、停止登记表）**原样保留**。

### 5.4 `sse.py`
- `run_agent_stream(agent_loop, session_id, new_user_text, stop_event)`：
  - 从 `HistoryStore.get(session_id)` 取历史，追加 user 消息；
  - `async for ev in agent_loop.run_stream(history, stop_event): yield ev`；
  - 事件名与字段**完全不变**（`text_delta/thinking_delta/tool_start/tool_result/chart`）。
- `detect_artifacts` 原样保留（对工具结果文本抽 chart/table）。

### 5.5 `compaction.py`
- 全部 `aget_state/aupdate_state/RemoveMessage/REMOVE_ALL_MESSAGES` 换成：
  ```python
  history = await store.get(session_id)
  # ...计算切点...
  await store.set(session_id, [summary_msg, *preserved])
  ```
- 软线/硬线算法本身**不变**（token 阈值、保留策略、可见化落库）。
- `HumanMessage(...)` → `{"role":"user","content":...}`（摘要消息）
- `_safe_keep_from` 判断 `ToolMessage` → 判断 `m.get("role")=="tool"`。

### 5.6 `mcp_client.py`
- `StructuredTool.from_function(...)` → `Tool(name, description, schema, call)`。
- fastmcp 连接/发现/合并/`_call_tool_safe` 全部保留。

### 5.7 `routers/sessions.py`
- `hub.saver.adelete_thread(session_id)` → `await history_store.delete(session_id)`。

### 5.8 `tokens.py`（可选增强）
- 现版本是纯字符估算；建议对齐 GA `core/tokens.py`：
  「上次真实 usage 为基准 + 其后新增消息字符估算」，用 `chat_stream` 返回的 `raw_usage` 校准。
- 本轮可先保留现有粗估，P0 后再优化（不影响正确性，只影响压缩时机精度）。

### 5.9 `routers/chat.py`
- 若压缩/自愈/流式入口签名保持不变，**基本不用改**；只改内部 import 来源。

## 6. 历史数据迁移

### 6.1 迁移脚本（一次性，`backend/scripts/migrate_checkpoints.py`）
1. 备份：`copy app.db app.db.premigrate.<ts>`，同时 `PRAGMA wal_checkpoint(TRUNCATE)` 后再拷。
2. 建 `session_history` 表。
3. 遍历 `checkpoints` 表，按 `thread_id` 取 `checkpoint_id` 最大（最新）的一条；
   BLOB 是 LangGraph 的 checkpoint（msgpack）。用 `langgraph.checkpoint.serde` 或
   `msgpack` 解出 `channel_values.messages`。
4. 转成 dict 列表（AIMessage→assistant、HumanMessage→user、ToolMessage→tool、SystemMessage→system）。
5. 写入 `session_history`。
6. 校验：抽 5 个会话，对比迁移前后「消息条数/首尾内容」一致。

> ⚠️ 迁移脚本运行时机：**在删除 langgraph 依赖之前**跑（因为要用它的 serde 解包）。
> 这是「先迁数据，再删依赖」的顺序要求。

### 6.2 双读兜底
- 迁移完成后，`HistoryStore.get()` 若 `session_history` 无该会话，再尝试从 `messages` 表
  （按 role/content 顺序）重建一份近似历史（丢失 tool_calls 结构，但保证可继续对话）。


---

## 7. 分阶段实施步骤（每步可独立验证）

### 阶段 0：准备与基线
- [ ] 备份 `backend/data/app.db`（含 wal/shm）：`sqlite3 app.db "PRAGMA wal_checkpoint(TRUNCATE)"` 后整体复制。
- [ ] 记录基线：现有功能逐项手测一遍（登录/建会话/多轮/工具/图表/压缩/停止/删除），留截图。
- [ ] 记录版本：`pip freeze > work/freeze.before.txt`。
- 产物：基线清单 + 备份文件。

### 阶段 1：`tools.py` + `mcp_client.py` 改造（最低风险，先热身）
- [ ] 新增 `tools.py`（`Tool` / `ToolRegistry`）。
- [ ] `mcp_client.MCPToolset._wrap` 返回 `Tool`；`tools` 属性类型改 `list[Tool]`；`describe()` 不变。
- [ ] `agent.py` 里 `tools=self.toolset.tools` 处先做适配（此时 `create_agent` 还能接受自研 Tool 吗？
      → 用 `to_openai_schema()` 转成 dict 传入）。
- 验证：`GET /api/mcp/tools` 正常；旧 agent 仍能调用工具。
- **回滚点**：git commit。

### 阶段 2：`llm_client.py` 上线（核心难点）
- [ ] 新增 `llm_client.py`，先用独立脚本对真实模型跑通两线（chat / responses）。
- [ ] 单测：普通文本、reasoning、tool_calls 增量拼接、空 chunk 跳过。
- [ ] `llm.py` 增加 `build_client*`，与旧 `build_chat_model*` **并存**（feature flag：
      `AGENT_USE_NATIVE_LLM=1` 走新客户端）。
- 验证：脚本级通过；再切 flag 跑一次完整对话，对比输出一致。
- **回滚点**：git commit。

### 阶段 3：`history_store.py` + 迁移（风险最高，务必先备份）
- [ ] 建表 + `HistoryStore`。
- [ ] 写迁移脚本，**在 langgraph 依赖还在时**跑（用其 serde 解 checkpoint）。
- [ ] 校验 5 个抽样会话。
- 验证：`history_store.get(session_id)` 能还原出与旧 checkpoint 一致的 message 序列。
- **回滚点**：git commit；数据可回滚到 `.premigrate` 备份。

### 阶段 4：`agent_loop.py` + `sse.py` + `agent.py`/`runtime.py` 接线
- [ ] 新增 `AgentLoop`，实现 while + dispatch + 事件 yield + 悬空自愈。
- [ ] `sse.run_agent_stream` 改为消费 `AgentLoop`。
- [ ] `agent.UserRuntime.build` 用 `LLMClient + ToolRegistry + AgentLoop`。
- [ ] `runtime.RuntimeHub` 去掉 saver，改用 `HistoryStore`。
- [ ] `compaction.py` 改操作 `HistoryStore` 的 dict 历史。
- 验证：完整功能回归（对标阶段 0 基线）。
- **回滚点**：git commit（此时旧代码还没删，可随时切回）。

### 阶段 5：删除旧代码与依赖
- [ ] 删除 `agent.py` 里 `create_agent`/`AsyncSqliteSaver` 残留；
- [ ] 删除 `requirements.txt` 里 5 个库，加 `httpx`；
- [ ] 全仓 `grep -rn "langchain\|langgraph" backend --include=*.py` 清理（保留必要的注释说明除外）；
- [ ] `pip install -r backend/requirements.txt` 干净环境验证。
- 验证：全新 venv 安装 + 启动 + 全功能回归。
- **回滚点**：git tag `p0-no-langchain`。

### 阶段 6：收尾
- [ ] 更新 `README.md` 技术栈段落（LangGraph/LangChain → 自研内核）。
- [ ] 更新 `AGENTS.md` 相关表述（如有）。
- [ ] 新增 `docs/20261105-changelog-P0.md`。
- [ ] 前端可选：`TOKEN_KEY` 由 `langgraph_token` 改为 `app_token`（同时兼容读取旧 key，避免用户被登出）。

## 8. 风险与对策

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| Responses 线流式解析与现库行为不一致 | 中 | 文本/思考显示异常 | 先用脚本对齐两线；保留 `use_responses_api` 开关；逐字段比对 |
| checkpoint 迁移丢历史/结构 | 中 | 老会话打不开或错乱 | 先备份；迁移脚本带校验；保留 `messages` 双读兜底 |
| 工具调用增量拼接错误 | 中 | 工具参数错 | 严格按 `index` 合并；单测覆盖多工具并发 |
| 并发写历史竞态 | 低 | 历史错乱 | 会话级 `asyncio.Lock` |
| 前端契约被破坏 | 低 | 页面报错 | SSE 事件名/字段不许变；联调前先跑前端 |
| 性能下降（httpx 未连接池） | 低 | 稍慢 | 用 `httpx.AsyncClient` 单例复用连接 |

## 9. 测试计划

| 测试 | 类型 | 内容 |
|---|---|---|
| `test_llm_client.py` | 单测/脚本 | 两线流式、reasoning、tool_calls |
| `test_tools.py` | 单测 | 注册、schema 生成、错误转文本 |
| `test_agent_loop.py` | 单测（mock LLM） | 多轮工具循环、max_turns、stop、悬空自愈 |
| `test_history_store.py` | 单测 | 读写删、重建、迁移 |
| `test_migrate.py` | 集成 | 抽样会话迁移前后一致 |
| E2E | 手工/脚本 | 对标阶段 0 基线全流程 |

## 10. 完成判据（与决策记录 §6 一致）

- [ ] requirements 无 langchain/langgraph；
- [ ] grep 后端无 langchain/langgraph import；
- [ ] 干净环境可装可跑；
- [ ] 全功能回归通过（对话/流式/思考/工具/图表/压缩/停止/删除/resume）；
- [ ] 老会话迁移后可继续对话；
- [ ] 前端零改动可用。

## 11. 后续（P1+，不在本轮）

见决策记录 §8 路线图。本实施文档在 P0 结束时会归档，P1 起另立文档。

---

## 附：本轮不动的文件（明确边界）

- `backend/app/auth.py`、`backend/app/db.py`（业务表）、`backend/app/routers/auth.py`、
  `backend/app/routers/me.py`、`backend/app/mcp_config.py`、`backend/mcp_server/server.py`、
  `backend/app/main.py`、`backend/app/config.py`、`backend/app/schemas.py`、前端全部。
- 仅当出现「签名不兼容」时才做最小适配，不做顺手重构。
