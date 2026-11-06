# P0 变更记录：去 LangChain / LangGraph 已完成

- 日期：2026-11-05
- 状态：**已完成并通过自测**
- 关联：`20261105-去LangChain依赖-决策记录.md`、`20261105-去LangChain依赖-实施文档.md`

---

## 1. 一句话结论

后端已彻底移除 `langchain / langchain-core / langchain-openai / langgraph /
langgraph-checkpoint-sqlite` 的**运行时依赖与 import**，换成 4 个自研模块；
**SSE 事件协议、HTTP API 路径与响应、前端契约均未改变**；89 个历史会话已从
langgraph checkpoint 解码迁移到新的 `session_history` 表，真实 LLM E2E 与单测全部通过。

## 2. 依赖变化

`backend/requirements.txt`：

- 删除：`langchain`、`langchain-core`、`langchain-openai`、`langgraph`、`langgraph-checkpoint-sqlite`
- 新增：`httpx==0.28.1`（自研 LLM 客户端直连模型，支持 `openai_chat` 与 `openai_responses` 两线）

验证命令（后端目录内应无任何 import）：

```bash
grep -rn "from langchain\|import langchain\|from langgraph\|import langgraph" backend --include=*.py
# 期望：无输出
```

> 唯一例外：`backend/scripts/migrate_checkpoints.py` 内部 import 了一次
> `langgraph.checkpoint.serde.jsonplus.JsonPlusSerializer`，**仅用于一次性解码旧 checkpoint**，
> 应用运行时不加载它。等确认无需再迁移历史后，可以删除该脚本。

## 3. 新增的自研内核模块

| 文件 | 职责 |
|---|---|
| `backend/app/llm_client.py` | httpx 异步客户端；`LLMClient.run(messages, tools, on_delta, stop_check) -> ChatResult`；chat/responses 双线流式、reasoning 增量、tool_calls 按 index 合并、usage 归一化、provider 错误提取与重试 |
| `backend/app/tools.py` | `Tool(name, description, input_schema, call)` + `ToolRegistry`；`schemas()` 生成 OpenAI tools；`dispatch()` **永不抛异常**，把错误转成可读文本回喂模型 |
| `backend/app/history_store.py` | `session_history(session_id PK, history JSON, updated_at)`；`get/set/append/delete/exists`，每会话 `asyncio.Lock` 串行化 |
| `backend/app/agent_loop.py` | `AgentLoop.run_stream(history, stop_event, session_id)`：while 主循环 + 悬空 `tool_call` 自愈（重跑或截断） + 事件 yield |

## 4. 改造的接线文件

| 文件 | 变更要点 |
|---|---|
| `llm.py` | `build_client()` / `build_client_from_config()` 返回 `LLMClient`（不再构造 `ChatOpenAI`） |
| `agent.py` | `BuiltModel` 持 `LLMClient + model_name`；`UserRuntime` 组装 `LLMClient + ToolRegistry + AgentLoop`；`sanitize_pending_tool_calls` 走 `HistoryStore` |
| `runtime.py` | `RuntimeHub` 持 `HistoryStore`；删除 `AsyncSqliteSaver`；停止登记表原样保留 |
| `sse.py` | `run_agent_stream(ur, session_id, user_text, stop_event)` 消费 `AgentLoop` 事件；`detect_artifacts`（图表识别）保留 |
| `compaction.py` | 全改为操作 dict history；软线自动摘要 / 硬线裁剪 / 可见化落库逻辑保留 |
| `tokens.py` | `message_text` / `is_summary_message` 兼容 dict 消息 |
| `mcp_client.py` | `StructuredTool` → 自研 `Tool`；fastmcp 连接/发现/多服务器合并逻辑不变 |
| `routers/chat.py` | 适配新的 `run_agent_stream` 调用；`done` 事件 `model` 字段取 `ur.model.model_name` |
| `routers/sessions.py` | 删除会话时同步 `hub.store.delete(session_id)` |

## 5. 历史数据迁移

脚本：`backend/scripts/migrate_checkpoints.py`

- 运行前提：在 **langgraph 仍安装**的环境执行（只为解码 msgpack blob）。
- 安全：运行前自动 `PRAGMA wal_checkpoint(TRUNCATE)` 并复制 `app.db` 为
  `app.db.premigrate.<时间戳>`（同时复制 `-wal` / `-shm`）。
- 逻辑：每个 `thread_id` 取最新 checkpoint → `JsonPlusSerializer.loads_typed` 解码 →
  取 `channel_values.messages` → 转成 OpenAI 风格 dict（含 `reasoning` 与 `tool_calls`）→
  规范化 tool_call/tool 配对（丢弃孤立 tool 消息）→ 写入 `session_history`。
- 实测结果：**89 个会话全部迁移，631 条消息，0 解码错误**。
- 支持 `--dry-run`（只解码不写库）与 `--sample N`（抽样打印首尾内容）。

```bash
cd backend
python scripts/migrate_checkpoints.py --dry-run   # 先看结果
python scripts/migrate_checkpoints.py             # 真正迁移（自动备份）
```


## 6. 自测记录（2026-11-05）

### 6.1 单元测试（12 项，全通过）

```bash
cd backend
python -m pytest tests -q      # 12 passed
```

覆盖：ToolRegistry schema 形状 / dispatch 不抛异常 / 非 dict 参数归一化、
AgentLoop.sanitize（补跑已知工具、丢弃未知悬空调用、已配对时不动）、`_safe_json`、
`detect_artifacts`（纯 JSON / MCP 文本包装 / 纯文本）、tokens（CJK 估算、摘要识别、块拼接）、
HistoryStore CRUD（临时库）、`run_agent_stream`（artifact → chart 事件、用户消息先入历史）、
`_safe_keep_from`（压缩不切断 assistant/tool 配对）。

### 6.1.1 硬性证明：屏蔽 langchain/langgraph 后仍能启动

用 `sys.meta_path` 拦截器把 `langchain*` / `langgraph*` 的 import 全部改成抛 `ImportError`，
再导入 `app.main` 与全部运行时模块：**导入成功、且 `sys.modules` 中没有任何 langchain/langgraph 模块**。
这从根上证明应用运行时不依赖它们（不是“装了但没用到”，而是“屏蔽了也能跑”）。

### 6.2 真实 LLM E2E（test 账号）

| 用例 | 结果 |
|---|---|
| 纯文本「只回复两个字：你好」 | `text_delta` 流式返回「你好」，`done.status=complete` ✅ |
| 工具调用「用 calculator 算 17*23」 | `tool_start(calculator)` → `tool_result("391")` → 文本「391」；DB 中 assistant 块含 tool 卡片 ✅ |
| `openai_responses` 线（glm-5.3-flash） | 正常流式返回「你好」✅ |
| 历史会话 resume（多轮继续） | 从 `session_history` 续接，上下文正确 ✅ |
| 停止生成 | `POST /stop` → `stopped:1`，assistant 消息 status=`stopped` ✅ |
| 手动压缩 `/compact` | 3 条旧消息 → 摘要，history 变 2 条，此后可继续对话 ✅ |
| 自动软线压缩 | 把 `auto_compact_tokens` 设 40 后第 3 轮触发，`compaction` 事件 + 可见 system 摘要 ✅ |
| 删除会话 | `DELETE /sessions/{id}` → 404 复查 + `session_history` 行清零 ✅ |
| 前端 | `npm run build` 成功，标题/文案已去品牌，`<title>Web Agent</title>` ✅ |

### 6.3 已知遗留 / 非阻塞项

- `config.py` 与 `llm_client.py` 各有一份 `LLMSettings`（字段基本一致，`llm.py` 做显式映射）。
  后续可合并成一个类型，属清理项。
- `frontend/src/api.ts` 的 token key 已从 `langgraph_token` 改为 `app_token`，
  并保留读旧 key 的兼容分支（老用户不会掉登录）。
- `backend/requirements.txt` 已不含 langchain；但**当前开发环境仍装着 langgraph**（历史遗留），
  这不影响运行。若要让环境彻底干净，可执行 `pip uninstall langchain langchain-core
  langchain-openai langgraph langgraph-checkpoint-sqlite`——**先确认不再需要迁移旧 checkpoint**。

### 6.4 自测数据清理

E2E 期间临时注册的 `selftest_* / e2e_*` 账号与临时会话已全部清理：数据库先从
`app.db.premigrate.20261105-130544` 还原为迁移前状态，再重新执行一次迁移脚本，
因此当前库 = **原始 41 用户 / 102 会话 / 469 条展示消息 / 89 个会话历史**，无自测残留。

> 旧 `checkpoints` / `writes` 表**保留未删**（遵守「不丢历史数据」）；应用已不再读写它们，
> 确认无需回滚后可以自行 `DROP`。

## 7. 如何快速复现自测

```bash
# 1) 后端依赖与单测
cd backend
pip install -r requirements.txt
python -m pytest tests -q

# 2) 前端构建
cd ../frontend
npm install
npm run build

# 3) 启动（单端口托管前端）
cd ../backend
python run.py

# 4) 登录拿 token
curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"identifier":"test","password":"12345678"}'
```

然后用返回的 `access_token` 调 `/api/sessions` 建会话、`POST /api/sessions/{id}/messages`
（`Accept: text/event-stream`）发消息即可看到 SSE 流。

## 8. 后续路线（P1 起）

浏览器控制暂不做。按 `20261105-去LangChain依赖-决策记录.md` 的 P0~P8 继续：
代码执行 → 文件编辑 → subagent/team/task → 计划模式 → goal 机制 → hooks 调度。
每加一项能力都优先复用本 P0 的 `Tool / ToolRegistry / AgentLoop / HistoryStore` 四件套。
