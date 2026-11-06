# 去 LangChain / LangGraph 解耦方案（评估与建议）

> 配套文档：`docs/20261105-LangChain-LangGraph依赖审计.md`（先读那份，看清「用在哪、为什么可替换」）。
> 本文回答：**如果决定不引入这两个库，具体怎么拆、工作量多大、有什么坑、推荐什么路线。**
>
> 前提说明：本仓库当前是从 `jiakuan_langchain` 复制来的**旧形态快照**，`AGENTS.md` 里还写着
> 「本项目 = Claude Code 源码存档 + 可读化」。但用户已明确：**后续要在本项目开展新的开发任务**，
> 且**不想引入 langchain / langgraph**。本文按这个新目标给建议，不动任何代码。

---

## 1. 先给结论

1. **可以拆，而且不亏。** 真正依赖库的能力只有 6 项（见审计文档 §0），GenericAgent 全有等价实现。
2. **不建议「直接 import GenericAgent 的 loop/handler」。** 它们与 GA 的 CLI 架构深度耦合
   （`handler.py` 14 万字符、`app.py` 15 万字符），搬进来会把 CLI 的包袱一并带进 Web 服务。
3. **推荐路线：抽取「最小自研内核」** —— 用 ~600~900 行新代码，实现「模型客户端 + while 循环 +
   JSON 会话存储 + SSE + 压缩」，形态参照 GA 的**设计**而非代码。这样既摆脱两个库，又不背 GA 的耦合。
4. **迁移风险最高的是持久化（checkpoint → 自有存储）**，其余层都是低风险。

---

## 2. 解耦工作量分解（按层给估）

| 层 | 现有实现 | 目标实现 | 代码量估算 | 风险 | 阻塞关系 |
|---|---|---|---|---|---|
| L1 模型客户端 | `ChatOpenAI` + `extract_text/reasoning` | 自研 `LLMClient`（httpx/aiohttp，处理 chat+responses 两线，流式 chunk） | 200~300 行 | 中（协议细节） | 无 |
| L2 Agent 循环 | `create_agent` + `astream_events` | 自研 `AgentLoop`（while + tool dispatch + yield 事件） | 150~250 行 | 低 | 依赖 L1 |
| L3 工具抽象 | `StructuredTool` | 裸 JSON schema + async dispatch | 80~120 行 | 低 | 无（复用 mcp_client 的 fastmcp 部分） |
| L4 会话持久化 | `AsyncSqliteSaver`（checkpoints/writes） | 复用现有 `messages` 表 + 新增 `history` 列/表 | 100~200 行 | **中高**（历史数据迁移） | 依赖 L2 定消息结构 |
| L5 压缩 | 自研 `compaction.py`（借状态 API） | 自研（改成操作自有 history list） | 改动 ~80 行 | 低 | 依赖 L2/L4 |
| L6 SSE | `astream_events` 事件 → SSE | 自研循环直接 yield → SSE | 改动 ~60 行 | 低 | 依赖 L2 |
| L7 RuntimeHub/鉴权/多用户 | 自研 | 不变 | 0 | 无 | 无 |

**合计新增/改动：约 700~1100 行**，集中在 L1/L2。L7（多用户、鉴权、MCP 装配）完全不动。

---

## 3. 推荐迁移顺序（每步都可独立验证）

按「先易后难、先无状态后有状态」排：

### 阶段 A：先拆工具抽象（L3）—— 零风险热身
- 目标：`mcp_client.py` 里的 `StructuredTool.from_function` 换成自研的轻量工具对象
  （`name / description / input_schema / async __call__`）。
- 验证：`GET /api/mcp/tools` 返回结构不变；agent 仍能调用工具。
- 收益：先切断一个 import，建立信心。

### 阶段 B：拆模型客户端（L1）—— 核心难点
- 目标：新增 `llm_client.py`，用 `httpx`（或 `aiohttp`）直接实现：
  - `wire_api == openai_chat` → `/v1/chat/completions`
  - `wire_api == openai_responses` → `/v1/responses`
  - 流式解析 SSE，统一成 `{"type":"text"/"reasoning"/"tool_call_delta"}` 事件
  - 参考 GA `genericagent/llm/core.py` 的 `NativeOAISession`（三线归一化 + `_record_usage`）
- 验证：先用一个脚本对着现有 `llm.yaml` 跑通「发一句、收流式文本」。
- 注意坑：responses 线的 `content` 块列表形态、reasoning 在 `summary[].text`、tool_call 增量拼接。

### 阶段 C：拆 Agent 主循环（L2）—— 替换 `create_agent`
- 目标：新增 `agent_loop.py`，实现：
  ```
  messages = [system, user]
  for turn in range(max_turns):
      resp = await llm.chat(messages, tools=schemas)   # 流式
      if not resp.tool_calls: break
      for tc in resp.tool_calls:
          result = await dispatch(tc.name, tc.args)     # 复用 mcp_client
          messages.append(tool_result(tc.id, result))
  ```
  边跑边 `yield` 事件（text_delta / tool_start / tool_result）。
- 验证：和现有行为对照（同一 prompt、同一工具，输出与工具卡片一致）。
- 这一步做完，`sse.py` 改成直接消费 `AgentLoop` 的 yield 即可（L6 顺带完成）。

### 阶段 D：拆持久化（L4）—— 风险最高，最后做
- 目标：不再用 `AsyncSqliteSaver`。把「agent 可恢复的历史」落到自有表：
  - 方案 1（推荐）：给 `messages` 表加 `extra` 或新建 `turns` 表，按 turn 存 `history_before/after`（仿 GA transcript）。
  - 方案 2：给 `sessions` 表加 `history` JSON 列，整段存。
- **历史数据迁移**：现有 `app.db` 里 `checkpoints/writes` 有 1037/1471 行（89 个会话）。
  迁移脚本可读 checkpoint BLOB（msgpack）还原 `messages`，写进新结构。**这是唯一需要写一次性迁移脚本的地方。**
- 验证：老会话能 resume、能继续对话、能压缩、能删除。

---

## 4. 明确「不要做什么」（避坑清单）

1. **不要直接把 GA 的 `agent_runner_loop` + `handler.dispatch` 抄进来。**
   - `loop.py` 本身不复杂（~300 行），但它调用 `handler.dispatch`，而 `handler` 是一个 14 万字符的
     巨类（含所有 CLI 工具：浏览器、代码执行、子代理控制、ask_user……）。
   - 正确做法：只借它的**循环骨架**，工具层继续用本项目已有的 `mcp_client`。
2. **不要为了保留 `astream_events` 而保留整个 LangGraph。** 事件流是循环里 `yield` 就能产出的东西。
3. **不要在拆 L1 时按记忆写 Responses 协议。** 本项目 `llm.py` 注释里已经有实测结论（chunk 块列表、
   reasoning 在 `summary[].text`），务必以代码里的实测为准，别信默认认知。
4. **不要一次性全删。** 按阶段 A→B→C→D 走，每阶段都能跑现有前端。可以新旧并存（feature flag 切换）。
5. **持久化迁移前先备份 `app.db`。** 它有 WAL，先 checkpoint 再拷，别只拷 `.db`（否则丢 `-wal` 里的写入）。

---

## 5. 依赖对比（拆完之后 requirements 会变成什么样）

拆完 L1~L4 后，`backend/requirements.txt` 可以从这样：

```
langchain==1.4.8
langchain-core==1.7.4
langchain-openai==1.7.3
langgraph==1.2.19
langgraph-checkpoint-sqlite==3.1.3
fastmcp==2.14.4
mcp==1.26.0
fastapi / uvicorn / python-multipart / pyyaml / aiosqlite / pydantic / pyjwt / bcrypt
```

变成：

```
httpx            # 或 aiohttp，替换 ChatOpenAI 的底层
fastmcp==2.14.4  # 保留：MCP 接入
mcp==1.26.0
fastapi / uvicorn / python-multipart / pyyaml / aiosqlite / pydantic / pyjwt / bcrypt
```

净减少 5 个顶层依赖（连带 20+ 传递依赖）。**前端完全不用动**（它零依赖这两个库）。

---

## 6. 如果还想更进一步：直接「把 GenericAgent 的思想搬进 Web 形态」

如果目标不只是「去掉两个库」，而是「把本项目建设成 GenericAgent 的 Web 多用户版」，那可以考虑：

- **后端内核**：以 GA 的 `llm/core.py`（模型）+ `loop.py`（循环）+ `context.py`（压缩）为蓝本，
  重写成本项目自己的 `backend/agent_core/`（而不是 import GA 包）。
- **subagent / 多智能体**：GA 的 `agents/manager.py`（spawn/wait/close/resume）、`team.py`、`swarm.py`、
  `mailbox.py`、`event_bus.py` 是现成的设计参考。Web 形态下可以简化为：
  - 一个 `SubagentManager`（进程内 asyncio task 或独立子进程）
  - 一个团队注册表
  - 父子消息总线
- **注意**：GA 的 subagent 是**基于独立 OS 进程 + 文件 IPC** 的（`subprocess.Popen` + `mailbox.jsonl`），
  这在 Web 服务里要改成「进程内 task 池」或「独立 worker + 队列」，不能照搬进程模型。

> 这一层是**新功能**，不是「去依赖」的必要部分。建议先把 L1~L4 拆干净、跑稳，再谈 subagent。

---

## 7. 时间与决策建议

| 选项 | 工作量 | 风险 | 适合场景 |
|---|---|---|---|
| **A. 维持现状** | 0 | 版本升级持续踩坑 | 只想尽快交付、不介意依赖 |
| **B. 分阶段拆 L3→L1→L2→L4**（本文推荐） | 3~5 天 | 低（每阶段可验证） | 想彻底去依赖、且要长期维护 |
| **C. 直接重写成 GA 风格内核** | 1~2 周 | 中（等于重做后端） | 目标就是「GA 的 Web 版」 |

**我的建议：走 B。** 理由：
- 你现在真正被这两个库「卡住」的地方（压缩、token、自愈）**本来就是自研的**，拆掉库不会损失能力；
- L1/L2 的替换有 GA 这个「参考答案」在旁边对照，风险可控；
- L7（多用户/鉴权/MCP）完全不动，前端完全不动，改造面被限制在 `backend/app` 的 7 个文件里。

---

## 8. 本文与审计文档的对应关系

| 审计文档章节 | 本文对应 |
|---|---|
| §2 使用位点（7 文件 12 行） | §2 工作量分解表的「现有实现」列 |
| §3.1 模型层 | 阶段 B |
| §3.2 主循环层 | 阶段 C |
| §3.3 持久化层 | 阶段 D |
| §4 绕过/对抗框架的证据 | 本文 §4「不要做什么」 |
| §5 缺 subagent | 本文 §6 |
