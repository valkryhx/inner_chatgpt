# LangChain / LangGraph 依赖审计（本项目现状 vs GenericAgent）

> 目的：回答一个具体问题——**`jiakuan_langchain` 到底把 langchain / langgraph 用在了哪一层？**
> 以及：在已经有 `D:\git_codes\GenericAgent`（纯手写、零 langchain 依赖、且已实现 agent 主循环 +
> subagent + 多智能体）的前提下，这两个库在本项目里是否真的不可替代。
>
> 结论先行：**用户的直觉是对的。** 本项目对这两个库的依赖是「薄且集中在替换成本最低的几层」，
> 并且项目已经在多处**绕过/对抗**框架的默认行为、用自研代码改写框架状态。详见下文。
>
> 审计基线：2026-11-05，代码 = 从 `D:\git_codes\jiakuan_langchain` 复制到本项目的快照。
> 审计方法：`grep` 全量 import + 逐文件阅读调用点 + 查 `backend/data/app.db` 实际表结构。

---

## 0. TL;DR（一句话结论表）

| 层 | 谁在做 | 库还是自研 | 去掉难度 |
|---|---|---|---|
| 模型 HTTP 协议（chat / responses 两线、流式 chunk） | `ChatOpenAI` | 库 | **低**（GenericAgent 有等价 `NativeOAISession`） |
| Agent 主循环（模型→工具→回填→再模型） | `create_agent`（内部编译成 `CompiledStateGraph`） | 库 | **低**（GA `agent_runner_loop` 就是同一套 while 循环） |
| 会话状态持久化 / resume | `AsyncSqliteSaver`（`checkpoints`/`writes` 两张表） | 库 | **中**（要迁一次历史数据；但本项目本来就在 `messages` 表重复存了一份） |
| 流式事件 | `astream_events(version="v2")` | 库 | **低**（直接 `yield` 即可） |
| 工具抽象 | `StructuredTool` | 库（薄） | **低**（GA 用裸 JSON schema） |
| 消息类型 | `HumanMessage`/`RemoveMessage`/`ToolMessage` | 库（基元） | **低**（等价于带 id 的 dict） |
| 上下文压缩 | **自研** `compaction.py`（借库状态 API 读写） | 自研为主 | 本来就自研 |
| 多用户 / 鉴权 / MCP 装配 / RuntimeHub | **自研** | 自研 | 与库无关 |

> 关键判断：**真正由库提供的「能力」，只有前 6 行，而且每一行在 GenericAgent 里都有成熟的手写实现。**
> 项目里最复杂、最花心思的部分（压缩、悬空 tool_call 自愈、多用户隔离）恰恰**不是**库给的。

---

## 1. 依赖清单与版本（本机实测）

```
langchain                    1.4.8
langchain-core               1.7.4
langchain-openai             1.7.3
langgraph                    1.2.19
langgraph-checkpoint-sqlite  3.1.3
```

- 全部在 `backend/requirements.txt` 用 `==` 精确锁定。
- 依赖体量：按本机 pip metadata，`langchain` 直接声明依赖 22 个包、`langgraph` 直接声明依赖 6 个包
  （再叠加 pydantic / langsmith / jsonpatch / tenacity / orjson 等）。这些都要为「一个 HTTP 客户端 +
  一个 while 循环 + 一个 JSON 存储」付出。
- 版本敏感点：项目中至少 6 条「避坑」注释（chunk 形态、`create_agent` 签名、`SummarizationMiddleware`
  行为、`AsyncSqliteSaver` 路径等），说明每次升级都要重新踩坑。

对比：`D:\git_codes\GenericAgent` 的 `requirements.txt` 里**完全没有** langchain / langgraph，
核心依赖只有 `requests / beautifulsoup4 / bottle / simple-websocket-server / PyYAML / pydantic / psutil / urllib3`
（MCP 为可选 `mcp` + `fastmcp`）。


---

## 2. 全量使用位点（精确到行）

`grep -rn "langchain\|langgraph" backend --include=*.py` 的真实结果（去掉 `__pycache__`）：

| 文件 | 行 | 内容 | 性质 |
|---|---|---|---|
| `backend/app/agent.py` | 15 | `from langchain.agents import create_agent` | **核心**：装配 agent |
| | 16 | `from langchain_core.messages import RemoveMessage` | 状态修复用 |
| | 18 | `from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver` | 持久化 |
| | 219 | `from langchain_core.messages import ToolMessage` | 补悬空工具结果 |
| `backend/app/compaction.py` | 15 | `HumanMessage, RemoveMessage, ToolMessage` | 压缩改写状态 |
| | 16 | `from langgraph.graph.message import REMOVE_ALL_MESSAGES` | 整体替换消息 |
| `backend/app/llm.py` | 11 | `from langchain_openai import ChatOpenAI` | 模型接入 |
| `backend/app/mcp_client.py` | 15 | `from langchain_core.tools import StructuredTool` | 工具包装 |
| `backend/app/runtime.py` | 7 | `AsyncSqliteSaver` | 全局单例 saver |
| `backend/app/auth.py` | 19 | 只是默认字符串里含 "langchain" 字样 | **误报**（不是 import） |
| `backend/app/config.py` | 18 | 只是路径注释含目录名 | **误报** |

> 精确统计：**真正的 import 只有 7 个文件、约 12 行**。前端只有一个变量名 `langgraph_token`（localStorage key）
> 和 `package.json` 里的项目名 `langgraph-agent-web`——**前端零依赖**。

### 2.1 这 7 个文件各自真正用了哪些 API

| 能力 | 具体 API | 调用点 |
|---|---|---|
| 装配 Agent | `create_agent(model, tools, system_prompt, checkpointer)` | `agent.py:148` |
| 模型接入 | `ChatOpenAI(...)` / `build_chat_model*` | `llm.py:37,138` |
| 工具包装 | `StructuredTool.from_function(...)` | `mcp_client.py:163` |
| 消息基元 | `HumanMessage / ToolMessage / RemoveMessage` | `compaction.py`, `agent.py` |
| 状态读写 | `agent.aget_state(cfg)` / `agent.aupdate_state(cfg, ...)` | `compaction.py`(3处), `agent.py`(2处) |
| 整体替换历史 | `REMOVE_ALL_MESSAGES` | `compaction.py:138,218,267` |
| 流式事件 | `agent.astream_events(..., version="v2")` | `sse.py:91` |
| checkpoint 存储 | `AsyncSqliteSaver.from_conn_string` / `adelete_thread` | `runtime.py:33`, `sessions.py:77` |


---

## 3. 逐层拆解：库到底承担了什么

### 3.1 模型接入层（`llm.py`）—— `ChatOpenAI`

- 职责：把 `llm.yaml` 或用户配置（base_url / api_key / model / wire_api）变成可流式调用的模型对象。
- 实际只用到两件事：**发 HTTP 请求** + **解析流式 chunk**。
- 项目还被迫自己写了两段「补丁式」解析：
  - `extract_text()`：处理 `use_responses_api=True` 时 `content` 是块列表而非字符串；
  - `extract_reasoning()`：从 `content` 的 `reasoning` 块 / `additional_kwargs.reasoning_content` /
    Responses 线 `summary[].text` 三种形态里抠思考文本。
- **GenericAgent 等价物**：`genericagent/llm/core.py` 的 `NativeOAISession` / `NativeClaudeSession`，
  同样直接打 HTTP，`wire_api` 显式映射，流式 chunk 自己归一化（`_record_usage` 统一三种 wire 的 token）。
  → **这一层完全可以由 GA 的 llm 层平替，且 GA 的协议覆盖更全（anthropic / openai_chat / openai_responses 三线一等支持）。**

### 3.2 Agent 主循环层（`agent.py` + `sse.py`）—— `create_agent` + `astream_events`

- `create_agent(...)` 的返回类型是 `CompiledStateGraph`（本机实测签名返回注解），即**一个 LangGraph 编译图**。
  它的本质就是业界通用的 ReAct 循环：`模型 → 若有 tool_calls 就执行 → 把 tool 结果回填 → 再问模型 → …`，
  直到模型不再调用工具。
- 项目通过 `astream_events(version="v2")` 拿 `on_chat_model_stream / on_tool_start / on_tool_end` 三类事件，
  在 `sse.py` 里归一化成前端 SSE 协议。
- `recursion_limit=48`（`agent.py:165`）只是给这个循环套了个最大步数，对齐 GA 的 `max_turns≈40`。
- **GenericAgent 等价物**：`genericagent/core/loop.py:201` 的 `agent_runner_loop(...)`——
  一个显式的 `while turn < max_turns` 循环，自己读 `response.tool_calls`、自己 `dispatch` 每个工具、
  自己把 `tool_results` 拼回下一条 message。**逻辑与 `create_agent` 内部图完全同构，只是 GA 把控制权留在自己手里。**
  → 这一层是本项目「看起来用了框架」的主体，但替换成本其实很低：一个 ~50 行的 while 循环即可。

### 3.3 会话持久化 / resume 层（`runtime.py` + `checkpoint` 表）—— `AsyncSqliteSaver`

- 这是**唯一真正有「替换成本」的一层**：LangGraph 的 checkpointer 把整个对话状态存进两张表
  （本机 `app.db` 实测）：
  - `checkpoints`（1037 行，按 `thread_id` 存状态快照 BLOB）
  - `writes`（1471 行，存中间 write）
  - `thread_id` = `session_id`，共 89 个 thread，与 `messages` 表的 89 个 session 一一对应。
- 但**本项目自己还有一套独立的 `messages` 表**（业务展示用：role / content / reasoning / blocks / status），
  也就是说「对话内容」被**存了两份**：一份在 `checkpoints`（给 agent 恢复用），一份在 `messages`（给前端展示用）。
  `routers/sessions.py` 删会话时还要**同时删两处**（`db.delete_session` + `saver.adelete_thread`）。
- **GenericAgent 等价物**：`genericagent/core/transcripts.py` + `sessions/_history.json` + `events.jsonl`——
  文件式 transcript，每个 turn 记录 `backend_history_before/after`，支持 resume。GA 的持久化是
  「**一份历史 + 事件流**」，没有双写问题。
  → 迁走这一层需要一次性迁移历史 checkpoint 数据，但**本项目已有 `messages` 表可作权威副本**，
    迁移面其实比想象中小。


---

## 4. 最有说服力的证据：项目一直在「绕过/对抗」这两个库

如果这两个库真的「必要且顺手」，代码不会出现下面这些现象。这些现象本身就是「依赖并非必要」的强证据：

### 4.1 自动压缩：弃用官方 `SummarizationMiddleware`，改成自研

`agent.py:140-146` 的注释原文：

> 自动压缩不再使用 `SummarizationMiddleware`。原因：它按「模型上报的总 token（含 system/tools，动辄数百）」
> 判定，且会把「已是摘要」的上下文再次摘要；手动压缩后 checkpoint 里长期存在一条摘要消息，
> 会被 sse 层误判成「每轮又压缩了一次」。改为在发送前用 `compaction.enforce_soft_limit` 按真实 token 阈值判断。

即：**框架自带的压缩中间件被判定为不好用，换成了自研的 `compaction.py`（273 行）。**
而 `compaction.py` 的算法（软线摘要 + 硬线裁剪）**明确写着「设计对齐 GenericAgent」**。

### 4.2 token 估算：自己写 `tokens.py`

`tokens.py` 完全自研（CJK 感知的粗略估算），framework 里也有 `token_counter`，但项目没用它。
注释里也直接说这是「供自研软线/硬线阈值判定使用」。

### 4.3 悬空 tool_call 自愈：手写 ~75 行去修框架留下的坏状态

`agent.py:168-242` 的 `sanitize_pending_tool_calls()`：当上一轮工具异常中断、checkpoint 里残留
「AI 发了 tool_calls 但没有对应 tool 结果」时，**手动 `aget_state` 读出来、找到悬空位、
要么补跑工具、要么用 `RemoveMessage` 截断**。这是典型的「框架没帮你兜住，只能自己补」。

### 4.4 压缩时直接操纵 `REMOVE_ALL_MESSAGES` 重写历史

`compaction.py` 三处用 `[RemoveMessage(id=REMOVE_ALL_MESSAGES), 摘要, *保留段]` 直接替换整个消息列表——
本质是把 LangGraph 的状态当成一个**可变 list 手动改**，而不是用框架提供的高层压缩能力。

### 4.5 工具错误：自己包一层 `_call_tool_safe`

`mcp_client.py` 把 MCP 工具调用全部包成「异常转文本回喂模型」，并且 `mcp_client.py:3` 明说
`langchain.mcp` 与 fastmcp 2.14.4 不兼容，只能手写 `fastmcp.Client` 薄封装。

> **小结**：本项目把 langchain/langgraph 当成「一个可被绕过的底座」在用——状态自己改、压缩自己写、
> 错误自己兜、token 自己算。这恰恰说明这些「能力」并没有真正被框架的价值兜住。


---

## 5. 这两个库**没有**提供什么（但项目想要）

用户提到 GenericAgent 有 **agent 主循环 + subagent + 多智能体**。本项目（`jiakuan_langchain`）：

- **有**：单 agent 的工具调用循环（这是 `create_agent` 给的）。
- **没有**：subagent / 多智能体 / 团队协作。全仓 `grep -i "subagent|swarm|team|multi-agent"` 在
  `backend/app` 与 `frontend/src` 下**零命中**。
- 也就是说：**当初选这两个库时想覆盖的「多智能体」场景，其实一个都没落地**；真正实现复杂的
  subagent / swarm / team 的是**不用这两个库的 GenericAgent**。

这形成一个非常有说服力的对照：

| 能力 | jiakuan_langchain（用 langchain/langgraph） | GenericAgent（不用） |
|---|---|---|
| agent 主循环 | ✅ `create_agent` | ✅ `agent_runner_loop` |
| 工具调用 / 错误自纠 | ✅（部分靠自研封装） | ✅（`dispatch` + `tool_before/after` hooks） |
| 上下文压缩 | ✅ 自研 | ✅ 自研（GA 更成熟：真实 usage 校准） |
| 会话持久化 / resume | ✅ checkpointer（双写） | ✅ 文件 transcript |
| **subagent** | ❌ **无** | ✅ `agents/manager.py`（spawn/wait/close/resume） |
| **多智能体 / team / swarm** | ❌ **无** | ✅ `agents/team.py`、`swarm.py`、`mailbox`、`event_bus` |
| MCP 接入 | ✅ fastmcp 薄封装 | ✅ `mcp/runtime.py`（更完整：连接池/缓存/热重载） |

> **结论**：论「能力覆盖」，GenericAgent 是超集；论「依赖负担」，本项目重得多。

---

## 6. 如果当初不用这两个库，会是什么样（反事实推算）

基于对 GA 代码的实际阅读，给出一份「等价替换」对照，说明替换不是空想：

| 现在（langchain/langgraph） | 可直接改用 GenericAgent 的等价实现 |
|---|---|
| `ChatOpenAI` + `extract_text/extract_reasoning` | `genericagent/llm/core.py` `NativeOAISession`/`NativeClaudeSession` |
| `create_agent` + `astream_events` | `genericagent/core/loop.py` `agent_runner_loop`（显式 while 循环，`yield` 流式） |
| `AsyncSqliteSaver`（checkpoints/writes） | `genericagent/core/transcripts.py` + `messages` 表（单一权威历史） |
| `StructuredTool` | 裸 JSON schema（GA `assets/tools_schema.json` 做法） |
| `RemoveMessage` / `REMOVE_ALL_MESSAGES` | 直接对 `history` list 做切片/替换 |
| 自研 `compaction.py` | `genericagent/core/context.py`（`should_auto_compact_agent` + `compact_agent_context`） |

> 注意：上面是**能力映射**，不是「照抄 GA 代码就能跑」。真正落地时需要评估：
> - GA 的 `loop.py` 与它的 `handler.py`（14 万字符）/ `app.py`（15 万字符）深度耦合，
>   直接搬会带进一大坨 CLI 特有的东西；
> - GA 是**单进程 CLI 形态**，多用户 Web 服务需要自己加 RuntimeHub / 隔离（但本项目这部分本就自研，与库无关）。
> 因此更现实的做法见下一份文档《解耦评估与建议》。


---

## 7. 什么情况下「留着也行」

公平起见，列出这两个库确实带来、且不容易白拿的价值：

1. **`astream_events` 的事件粒度**：它把「LLM token 流 / 工具开始 / 工具结束」统一成标准事件名，
   省去了自己在循环里埋 yield 的心智负担。GA 是靠 generator `yield` 手工约定的。
2. **checkpointer 的「快照/分支」语义**：LangGraph 的 checkpoint 天然支持时间旅行、分叉。
   如果未来要做「回滚到某个中间步骤再试另一条路」，这套现成的状态快照有价值。
   （但本项目目前只用了最基础的 resume + 删除，没用到分支/时间旅行。）
3. **生态与文档**：LangChain 的集成（向量库、retriever、各种 tool 封装）很多。
   如果后续要快速接一大堆现成集成，它省事。
   （但本项目目前只接了 OpenAI 兼容的 chat 模型 + 自建 MCP，用不到这个生态。）

> 换句话说：**如果本项目未来要做「LangGraph 式的时间旅行/分支对话」或「大量现成集成」，
> 留着的理由会变强；如果只是现在这个「聊天 + 工具 + 压缩 + 多用户」形态，留着是纯负担。**

---

## 8. 附：审计可复现命令

```bash
# 1) 全量使用位点
grep -rn "langchain\|langgraph" backend --include=*.py | grep -v __pycache__

# 2) 具体 API 使用
grep -rnE "create_agent|ChatOpenAI|StructuredTool|astream_events|aget_state|aupdate_state|AsyncSqliteSaver|REMOVE_ALL_MESSAGES|RemoveMessage" backend/app --include=*.py

# 3) checkpoint 表实际内容（证明双写）
python -c "import sqlite3;c=sqlite3.connect('backend/data/app.db');print([r for r in c.execute(\"select name from sqlite_master where type='table'\")])"

# 4) 确认 GenericAgent 无这两个依赖
grep -rin "langchain\|langgraph" D:/git_codes/GenericAgent/requirements.txt || echo "NONE"
```

---

## 9. 一句话给决策者

> **这两个库在本项目里只值「一个 HTTP 客户端 + 一个 while 循环 + 一个 JSON 存储」，
> 而这三样东西 GenericAgent 都已经用更简单、更可控的方式实现过了。
> 更关键的是：本项目为了用这两个库，反而写了一堆代码去绕开它们（压缩、token、自愈、状态替换），
> 还付出了 28 个直接依赖条目（外加更多传递依赖）和持续的版本踩坑成本。**
