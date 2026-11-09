# ACP Client 驱动本地 GenericAgent 设计与实施

> 日期：2026-11-09　状态：设计待批（已选定 B 路线）
> 一句话：给 `inner_chatgpt` 增加 ACP **client** 能力，把本机 GenericAgent（下称 GA）
> 当作「可后台运行、可流式观察、可追问、可停止」的外部子代理，为本项目的现有 agent 打下手。
> 协议对端为 `D:\git_codes\GenericAgent\frontends\acp\genericagent_acp_bridge.py`（564 行，ACP agent 侧）。

---

## 1. 背景与目标

### 1.1 现状

`inner_chatgpt` 已经是完整的 Web 多用户 Agent 服务：自研 `ToolRegistry`（`backend/app/tools.py`）、
显式工具循环（`agent_loop.py`）、进程内子代理（`subagent/`）、Agent Team-lite（`team_*`）、
Skill 渐进披露、MCP 工具接入、SSE 流式、停止级联、配额与 SQLite 持久化。

本项目铁律（`AGENTS.md` §0）：**借鉴 GenericAgent 的设计，但不 import GA 包**。
所以「用上 GA」不能是 `from genericagent.core.app import ...`，只能走进程间协议。

### 1.2 GA 在本项目中的角色

- GA 是**外援**：主 agent 把「自包含、可独立完成」的子任务派给它，它干完交回结果。
- 它也能承载**复杂任务**（长时间编码、跑测试、批量改文件），因此不能做成「一次调用阻塞到结束」。
- inner_chatgpt 自己仍然是主控：路由、上下文、对话体验、子代理/Team 都留在本项目，
  **不允许因为接入 GA 而让现有能力退化**。

### 1.3 为什么选 ACP

GA 已实现 ACP **agent 侧**（服务端）：stdio 上的 JSON-RPC，支持 `initialize` /
`session/new` / `session/prompt` / `session/cancel` / `session/set_mode` / `session/close`，
并主动推送 `session/update` 与 `session/request_permission`。仓库里还有一份可跑的
客户端参考实现 `GenericAgent/scripts/acp_client_demo.py`（308 行）。

选它而不是 HTTP / Python SDK / 直接引包，原因有四：

1. **进程隔离**：GA 跑在独立进程里，崩溃、卡死、内存泄漏都不拖垮 Web 服务；
2. **协议已存在**：不用改 GA 就能接，后续 GA 升级理论上不破坏契约；
3. **stdio 简单**：不需要端口、鉴权、跨机器网络暴露；
4. **双向流**：GA 的工具调用与文本能实时推回来，这是「复杂任务可观察」的前提。

---

## 2. 目标 / 非目标 / 反目标

### 2.1 目标（本期必须达成）

| 编号 | 目标 | 可验证的判据 |
|---|---|---|
| G1 | 模型能把子任务派给 GA，**不阻塞**主对话 | `ga_spawn` 立即返回 `agent_id`；主 SSE 继续输出 |
| G2 | 用户能实时看到 GA 的文本与工具步骤 | 消息流中出现可折叠卡片，含 `agent_message_chunk` 对应文本与 `tool_call` 行 |
| G3 | 同一 GA 会话可追问 | `ga_send` 后同一 `agent_id` 继续产出事件与结果 |
| G4 | `/stop` 能停掉 GA | `session/cancel` 先行，超时后杀进程树；卡片状态变 `stopped` |
| G5 | 多用户隔离与并发上限 | GA 进程的 cwd / `workspace_root` 为该用户 workspace；单用户并发有限 |
| G6 | 现有能力不退化 | 子代理 / Team / MCP / 文件工具全部沿用原实现，只做增量挂载 |

### 2.2 非目标（本期明确不做）

- 不做 ACP **server**：inner_chatgpt 不暴露给 Zed 等编辑器驱动。
- 不做 GA 权限弹窗 UI：固定 `full_access`，权限请求自动应答（见 §6.5）。
- 不做跨 generation 的 GA 会话持久化：本轮结束即关闭；跨轮续跑留给后续项目。
- 不做 `mcpServers` 透传：GA 的 bridge 目前忽略该字段（`bridge:226-262` 只取 `cwd`）。
- 不把 GA 的 `session/load` / `session/list` 接进来：bridge 明确返回 `-32601`（`bridge:496-499`）。

### 2.3 反目标（做出来就算失败）

- GA 的任务把主 SSE 卡住，前端只剩一个转圈。
- 为了复用工具名去改造 `SubagentHost` 的语义（侧链 history、能力裁剪、`fork_turns`），
  导致现有子代理行为变化。
- GA 进程被用户任务带出 workspace、或未注册进停止/配额体系，成为「杀不掉的幽灵进程」。
- 把 inner_chatgpt 的 LTM/API key 通过环境变量泄露给 GA 子进程。

---

## 3. 约束与已验证事实（全部来自源码，不是推测）

### 3.1 inner_chatgpt 侧

| # | 事实 | 坐标 | 对设计的影响 |
|---|---|---|---|
| I1 | `Tool.call` 返回单个 `str`，异常统一转可读文本 | `backend/app/tools.py:16-34`、`:59-75` | 工具不能自己吐流；流式必须走事件缓冲 |
| I2 | 主循环只在工具首尾发 `tool_start` / `tool_result` | `backend/app/agent_loop.py:259-274` | 同步委托会让长任务期间界面无信息 |
| I3 | 每轮派生 `TurnRuntime`：`EventBuffer` + `SubagentHost` + 编排工具 | `backend/app/subagent/session.py:86-90`、`:142-149` | **这是 ACP 工具的挂载点** |
| I4 | 子代理事件经 `buffer.stream()` 合并进同一条 SSE | `backend/app/routers/chat.py:29-118`、`:404` | GA 事件可复用同一条通道 |
| I5 | 前端已支持 `subagent_start/event/end` 与可折叠卡片 | `frontend/src/App.tsx:533-561`、`components/SubagentCard.tsx:51-136`、`types.ts:219-221` | 复用卡片 = 零前端协议改动 |
| I6 | 停止链路：协作旗 → `task.cancel` → `hub.stop_subagent_generation` → `process_registry.stop_generation` | `chat.py:589-629`、`runtime.py:45-56` | GA 进程必须注册 `ProcessLease` 才能被停 |
| I7 | `ProcessLease(user_id, session_id, generation_id, agent_id)` | `backend/app/process_registry.py:12-19`、`:115-124` | 直接复用，`agent_id="ga:<id>"` |
| I8 | Windows `--reload` 下是 SelectorEventLoop，`asyncio` 子进程不可用；`_ThreadProcess` 只包了 `communicate/wait/kill` | `process_registry.py:26-66` | **不能依赖 asyncio 子进程做双向管道**，见 §5.2 |
| I9 | 现有配额：用户/全局 `code_run`、`mcp_call`、`subagent` 信号量 | `backend/app/quota.py:25-35` | 新增 GA 会话信号量，语义对齐 |
| I10 | 清理顺序：同步释放 → `host.close_all()` → 取消 | `chat.py:155-205` | GA `close_all()` 挂同一处 |
| I11 | 后端 `permission` 相关代码零命中 | `grep -rn permission backend/app` | 没有权限 UI，ACP 权限必须自动应答 |
| I12 | 工具说明与 schema 每轮随 `registry.schemas()` 下发 | `agent_loop.py:199` | 只需把 GA 工具加进本轮 registry |

### 3.2 GenericAgent 侧（ACP 契约）

| # | 事实 | 坐标（`frontends/acp/genericagent_acp_bridge.py`） |
|---|---|---|
| G1 | 传输：stdio，**每行一个 JSON-RPC 2.0 消息**，UTF-8 | `:535-542`（`for raw_line in stdin`） |
| G2 | `protocolVersion = 1`，且无论客户端传什么都会回 1 | `:87-88`、`:202-206` |
| G3 | `initialize` 返回 `agentCapabilities` / `agentInfo` / `authMethods` | `:205-224` |
| G4 | `session/new` 取 `cwd`，用 `GeneraticAgent(workspace_root=cwd)` 建会话并起线程 | `:226-262` |
| G5 | `session/new` 返回 `modes.availableModes`：`read_only` / `ask` / `full_access` | `:245-262` |
| G6 | `session/prompt` 只处理 `text` / `resource_link` / `resource`；`image` 被替换为占位文本 | `:122-149`、`:370-423` |
| G7 | **同一 session 同时只允许一个 active prompt**，否则 `-32603` | `:385-391` |
| G8 | 流式：`session/update` + `agent_message_chunk`（文本增量） | `:99-104`、`:425-467` |
| G9 | 工具事件：`session/update` + `tool_call` / `tool_call_update` | `:330-368` |
| G10 | 权限：agent→client 的 `session/request_permission`，client 回 `{outcome:{outcome:"selected",optionId}}` | `:273-328` |
| G11 | `session/cancel` 是通知（无 id），内部调 `agent.abort()` | `:469-478` |
| G12 | `session/set_mode` 调 `set_permission_mode` 并回 `{modeId}` | `:500-506` |
| G13 | `session/close` 关闭 agent 并清理会话 | `:507-521` |
| G14 | `session/prompt` 的响应 `stopReason` 恒为 `end_turn`，异常只以 `[Bridge error]` 文本块出现 | `:393-421` |
| G15 | **每条消息都 `eprint("[ACP-BRIDGE] >>> ...")`**，stderr 不排空会写满管道阻塞 GA | `:181-191` |
| G16 | GA 设了 `workspace_root` 时：非 Python 的 shell 类 `code_run` 直接拒绝；Python 走 workspace guard | GA `genericagent/core/handler.py:84-86`、`:145-150`、`:947-956` |
| G17 | `--llm-no` 选择 GA 自己的 LLM 配置序号；缺省用 `active_profile` | bridge `:554-559`、`:193-200` |

### 3.3 版本与机器约束

- GA 是**自更新**的本地程序；bridge 行号与本设计绑定的是 2026-11-09 当前版本。
  实施时先记录 GA 的 `agentInfo.version` 与 bridge 文件 SHA256，写进会话元数据与日志。
- 本机 Windows；GA bridge 已做 `msvcrt.setmode(..., O_BINARY)` 与 stdout fd 重定向（`:14-45`），
  客户端的 stdout 解析必须是**按字节读、按 `\n` 切、UTF-8 解码**，不能依赖文本模式换行转换。

---

## 4. 方案比较

### A. 单个同步工具 `ga_delegate(task, timeout)`

模型调一次，后端拉起 bridge、跑完、返回最终文本。

- 优点：1–2 天；复用 `code_run` 的进程租约与超时。
- 缺点：命中反目标——长任务期间 `agent_loop` 只发首尾事件（I2），用户看不到任何进展；
  一次调用还会占满一个工具槽，模型无法中途调整。
- 结论：只适合作为 B 的“第一期最小闭环”，不单独作为最终形态。

### B. 后台 ACP 会话 + 一组工具（**选定**）

新增 `backend/app/acp/`，提供 `ga_spawn / ga_wait / ga_result / ga_send / ga_stop`；
GA 的 `session/update` 实时转成现有 `subagent_event`，复用 `SubagentCard` 与同一条 SSE。

- 优点：不阻塞；可观察；可追问；可停止；与现有子代理的心智模型一致（spawn → wait → result）；
  纯增量，不碰 `SubagentHost` / `TeamHost` 的任何不变量。
- 代价：4–6 天；需要自己写 stdio 传输层（I8）、事件映射、生命周期与测试。

### C. 把 GA 作为 `SubagentHost` 的第四种后端

复用 `spawn_agent / wait_agent / read_agent_result` 三个既有工具名。

- 优点：模型零新工具，UI / 取消 / 配额白捡。
- 缺点：`SubagentHost` 的语义绑死在进程内 `Runtime` 上——侧链 history（`subagent/store.py`）、
  能力闭世界裁剪（`subagent/capabilities.py`）、`fork_turns`、轮数统计、`subagent` 表落库，
  GA 一个都不满足。为了复用工具名去改 P2/P3 的不变量，正好踩中“能力退化”。
- 结论：否决。

**选定 B。** 理由：它是唯一同时满足「复杂任务可观察 + 可追问 + 可停止 + 不动现有子代理语义」的方案。
A 的闭环作为 B 的 Phase 1 自然发生，不写两套代码。

---

## 5. 架构

### 5.1 组件图

```text
主 LLM
  │  工具调用
  ▼
ga_spawn / ga_wait / ga_result / ga_send / ga_stop        (backend/app/acp/tools.py)
  │
  ▼
AcpSessionManager                                          (backend/app/acp/manager.py)
  ├─ 会话表 {ga_id -> GaSession}
  ├─ 配额信号量（每用户 / 全局）
  ├─ ProcessLease 注册（复用 process_registry）
  └─ 事件发布：buffer.put({"type":"subagent_start|subagent_event|subagent_end"})
  │
  ▼
AcpSession                                                 (backend/app/acp/session.py)
  ├─ AcpClient：JSON-RPC 请求/响应关联、权限自动应答
  └─ AcpProcess：Popen + stdout/stderr 读线程 + 写锁
  │
  ▼  stdin/stdout（行分隔 JSON）
GenericAgent bridge  →  GA agent 线程
  │
  ▼  session/update
EventBuffer → chat.py _merged_events → SSE → SubagentCard
```

### 5.2 传输层为什么自己写

- `asyncio.create_subprocess_exec` 在 Windows SelectorEventLoop（`uvicorn --reload`）不可用；
- 现有 `_ThreadProcess`（`process_registry.py:26-66`）只支持 `communicate()`，没有持续读写管道；
- ACP 需要**长期驻留的双向管道**，所以用自己的传输层：

```text
Popen(command, cwd=workspace, stdin=PIPE, stdout=PIPE, stderr=PIPE,
      bufsize=0, creationflags=CREATE_NO_WINDOW)   # 全程二进制
  ├─ stdout 读线程：readline() → 去 \r → json.loads → call_soon_threadsafe(dispatch)
  ├─ stderr 读线程：无条件排空（G15），写入环形缓冲最近 200 行
  └─ 异步写：asyncio.Lock 串行化，await asyncio.to_thread(stdin.write/flush)
```

关键点：

1. **stderr 必须持续排空**，否则 GA 的每条 `[ACP-BRIDGE] >>>` 日志会把管道写满（G15）。
2. stdout 读线程不能做重活；解析后只投递到事件循环，协议状态只在事件循环线程里改。
3. 关闭顺序：`session/close` → 等 1s → 关 stdin → 等进程退出 2s → `taskkill /T /F`（复用
   `process_registry._kill_tree` 的思路）。

### 5.3 新增文件

| 路径 | 职责 | 预计规模 |
|---|---|---|
| `backend/app/acp/__init__.py` | 包入口 | 极小 |
| `backend/app/acp/transport.py` | `AcpProcess`：Popen、读写线程、行解析、关闭 | ~200 行 |
| `backend/app/acp/client.py` | `AcpClient`：请求关联、通知分发、权限应答 | ~250 行 |
| `backend/app/acp/session.py` | `GaSession`：一次 GA 会话的状态机、事件映射、结果 | ~250 行 |
| `backend/app/acp/manager.py` | 会话表、配额、租约、GC、停止 | ~250 行 |
| `backend/app/acp/tools.py` | 5 个 Tool 定义 | ~200 行 |
| `backend/app/acp/config.py` | 环境变量与命令解析 | ~80 行 |
| `backend/tests/test_acp_*.py` | 单测 + 假 bridge 集成测试 | ~500 行 |

### 5.4 对现有文件的改动（全部为增量）

| 文件 | 改动 | 风险 |
|---|---|---|
| `backend/app/subagent/session.py` | `build_turn_runtime` 中构造 `AcpSessionManager`，把工具加入 `merged`，并放进 `TurnRuntime` 字段 | 低；失败时降级为无 GA 工具 |
| `backend/app/routers/chat.py` | `_merged_events` 的 active 判断同时看 GA manager；停止端点插入 `acp_manager.stop_generation`；清理块调用 `ga.stop_all_now()` + `await ga.close_all()`，并加进 `_settle_turn` 兜底 | 中低；停止顺序必须保持“优雅→硬杀” |
| `backend/app/config.py` | 新增 ACP 配置常量 | 无 |
| `frontend/src/components/SubagentCard.tsx` | `ROLE_LABEL` 增加 `ga: 'GA 外援'` | 无协议改动 |
| `backend/app/quota.py` | 新增 `user_acp_sessions` / `global_acp_sessions` 信号量 | 低 |

**不改**：`subagent/host.py`、`subagent/runtime.py`、`subagent/store.py`、`team_*`、`agent_loop.py`、
`tools.py`、SSE 事件名与字段。

---

## 6. 协议契约（客户端必须实现）

### 6.1 消息框架

- 一个 JSON-RPC 2.0 对象 = 一行；客户端写 `compact_json(msg) + "\n"`，UTF-8。
- 三种入站消息必须区分：

| 形态 | 判据 | 处理 |
|---|---|---|
| 响应 | 有 `id`、无 `method` | resolve `_pending[id]` |
| 通知 | 有 `method`、无 `id` | 按 `method` 分发（`session/update`） |
| 请求 | 有 `id` 且有 `method` | 必须回响应（`session/request_permission`） |

- 无法解析的行：记 stderr 缓冲并丢弃；**不能**因此中断读循环。
- 请求 id：客户端自增整数；权限请求的 id 由 GA 生成（`perm_<hex>`），原样回。

### 6.2 握手

```json
// → 
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
  "protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"inner_chatgpt","version":"1.0"}}}
// ←
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,
  "agentCapabilities":{...},"agentInfo":{"name":"genericagent-acp","version":"0.1.0"},"authMethods":[]}}
```

- 只接受 `protocolVersion == 1`；其它值记录告警但继续（GA 总是回 1）。
- 记录 `agentInfo.version` 到会话日志，供版本锚定。

### 6.3 建会话

```json
{"jsonrpc":"2.0","id":2,"method":"session/new",
 "params":{"cwd":"<用户 workspace 绝对路径>","mcpServers":[]}}
→ {"sessionId":"ga_<hex>","modes":{"currentModeId":"full_access",
    "availableModes":[{"id":"read_only"...},{"id":"ask"...},{"id":"full_access"...}]},"configOptions":null}
```

- `cwd` 必须是已存在的绝对目录，且在用户 workspace 内（服务端拼，不接受模型传入）。
- 若配置的 mode 与 `currentModeId` 不同，调 `session/set_mode`。

### 6.4 派任务与流

```json
{"jsonrpc":"2.0","id":3,"method":"session/prompt",
 "params":{"sessionId":"ga_...","prompt":[{"type":"text","text":"<自包含任务书>"}]}}
→ {"stopReason":"end_turn"}
```

- 同一 session 有 active prompt 时，客户端**必须先本地拒绝**（对应 G7），返回可读错误，
  不要依赖对端 `-32603`。
- `session/update` 的 `update` 字段是判别式：

| `sessionUpdate` | 关键字段 | 映射（见 §8） |
|---|---|---|
| `agent_message_chunk` | `content:{type:"text",text}` | `text_delta` |
| `tool_call` | `toolCallId`、`title`、`kind`、`status`、`rawInput.preview` | `tool_start` |
| `tool_call_update` | `toolCallId`、`status`、`content[0].content.text` | `tool_result` |

### 6.5 权限（自动应答）

GA 在 `ask` 模式下会发：

```json
{"jsonrpc":"2.0","id":"perm_<hex>","method":"session/request_permission","params":{
  "sessionId":"ga_...","toolCall":{"toolCallId":"...","title":"file_write","kind":"edit","status":"pending"},
  "options":[{"optionId":"allow_once","kind":"allow_once"},{"optionId":"reject_once","kind":"reject_once"}]}}
```

客户端固定回：

```json
{"jsonrpc":"2.0","id":"perm_<hex>","result":{"outcome":{"outcome":"selected","optionId":"allow_once"}}}
```

- 默认 mode 已经是 `full_access`，正常不会触发；此分支是**防死锁兜底**（GA 侧没有超时）。
- `INNER_ACP_PERMISSION=deny` 时可改成 `reject_once`，用于只读场景。
- 每次自动应答都往卡片发一对事件：`tool_start`（name=`permission:<title>`）+
  紧跟 `tool_result`（content=`allowed` / `denied`）。缺了 `tool_result`，
  `SubagentCard` 的步骤会永远停在 running 圆点。

### 6.6 取消与关闭

- `session/cancel`：通知，无响应。发出后等 2s；若 prompt 响应未回，杀进程树。
- `session/close`：请求，回 `{}`。会 `request_shutdown()`；随后关管道。
- 进程死亡：读线程 EOF → 所有 `_pending` future 以 `AcpTransportClosed` 失败，
  会话状态置 `errored`，发 `subagent_end{status:"errored"}`。

---

## 7. 工具契约

### 7.1 注入范围

只在 `build_turn_runtime` 里注入（`subagent/session.py:86-90` 同一处），因此：

- **root agent 有**；
- **子代理 / teammate 没有**（它们走 `SubagentRuntime.build`，不经过这里）。

这与现有编排工具“只有 root 有”的闭世界原则一致（`subagent/capabilities.py`），
避免一个任务里嵌套多层 GA 导致成本与并发失控。

### 7.2 五个工具

| 工具 | 入参 | 返回（JSON 文本） | 语义 |
|---|---|---|---|
| `ga_spawn` | `task`(必填, 自包含任务书), `title?`, `mode?`(`full_access`\|`read_only`\|`ask`), `timeout_s?`(默认 1800) | `{agent_id,status:"running",note}` | 建会话 + 发 `session/prompt`，后台跑 |
| `ga_wait` | `agent_id?`(缺省=等全部), `timeout_s?`(默认 60), `since_event_seq?` | `{agents:[{agent_id,status,last_seq,events_tail,result_preview}],timed_out}` | 等待完成或超时；**超时不会杀 GA** |
| `ga_result` | `agent_id`, `max_chars?`(默认 20000), `full?`(默认 false), `close?`(默认 false) | `{agent_id,status,prompts,result,stop_reason,tool_calls,error}` | 读取结果；`close=true` 顺手释放打开会话名额 |
| `ga_send` | `agent_id`, `message` | `{agent_id,status:"running"}` | 同一会话追问；需无 active prompt |
| `ga_stop` | `agent_id?`(缺省=全部) | `{stopped:[agent_id...]}` | `session/cancel` → 2s → 杀进程 |

约定（与 `ToolRegistry.dispatch` 一致）：

- 任何失败都返回 `[工具执行错误] ...` 文本，**永不抛异常**；
- `ga_spawn` 的任务书必须提醒模型：GA 看不到本对话，也不共享 inner 的 history，
  所以任务、路径、验收标准都要写全；
- `ga_wait` 超时返回 `status:"running"` 并提示「可继续 wait，或先 result 看中间文本」。
- `events_tail` 契约：每项 `{seq, kind:"text"|"tool_start"|"tool_result", name?, text?}`，
  `text` 截断 400 字符，默认返回最近 8 条；`seq` 是 manager 每会话单调自增的序号
  （不是 `EventBuffer` 的序号），`since_event_seq` 用它做增量。
- `ga_result` 的 `result` 语义见 §8.3；`prompts` 是该会话已完成的 prompt 次数。

### 7.3 工具描述（给模型看，节选）

```text
ga_spawn: 把一件可独立完成的子任务交给本机 GenericAgent（外部编码代理）。
  - 任务书要自包含：目标、涉及路径、验收标准、禁止事项；它看不到当前对话。
  - 它在本用户的 workspace 内工作，产物可直接用 file_read / code_run 复核。
  - 派活后必须用 ga_wait / ga_result 收结果；本轮结束时未完成的任务会被停止。
ga_wait: 等待 GA 完成。超时只表示还在跑，不会终止它。
ga_result: 读取 GA 的最终答复与工具调用统计。
ga_send: 对同一 GA 会话追加指令（一次只能有一个进行中的 prompt）。
ga_stop: 停止 GA（先发 session/cancel，2 秒后强制结束进程树）。
```

### 7.4 与既有工具的关系

GA 是“另一种执行路径”，不替代任何现有工具：

- 小任务仍应优先用 `file_read / file_write / code_run`（延迟低、结果可预期）；
- 需要多步、长时间、可能反复试错的编码任务，才交给 GA；
- GA 的产物落在同一 workspace，inner 可以用既有工具复核、继续修改、或打包下载。

---

## 8. 事件映射与前端

### 8.1 复用现有事件协议（零 SSE 字段改动）

GA 事件 → `EventBuffer` 里的 `subagent_*`：

| 时机 | 写入 buffer 的事件 |
|---|---|
| `ga_spawn` / `ga_send` 发起一次 prompt | `{"type":"subagent_start","agent_id":"ga_<hex>#<seq>","name":"GenericAgent · <title>","role":"ga","task":<task>}`（**一次 prompt 一张卡片**） |
| `agent_message_chunk` | `{"type":"subagent_event","agent_id":...,"event":{"type":"text_delta","text":...}}` |
| `tool_call` | `{"type":"subagent_event","agent_id":...,"event":{"type":"tool_start","id":<toolCallId>,"name":<title>,"input":{"preview":...}}}` |
| `tool_call_update` | `{"type":"subagent_event","agent_id":...,"event":{"type":"tool_result","id":<toolCallId>,"name":<title 或 "">,"content":<content.text 或 status>,"is_error":<status=="failed">}}` |
| prompt 返回 / 失败 / 停止 | `{"type":"subagent_end","agent_id":"ga_<hex>#<seq>","name":...,"status":"completed"\|"errored"\|"stopped","turns":0,"error":...}`；**有文本事件时不带 `result`**（避免与时间线重复展示） |

### 8.2 前端表现（复用 `SubagentCard`）

```text
▸ GA 外援 · GenericAgent · 修复登录超时        running · GA 外援 · 3 个工具
  任务：修复 backend/app/auth.py 的 token 刷新…
  ├ ● 正在读取 backend/app/auth.py          read
  ├ ● 已运行 pytest tests/test_auth.py      execute
  └ 文本：已定位到 refresh 分支…（流式追加，Markdown 渲染）
```

- `role="ga"` 只需在 `ROLE_LABEL` 加一行（`SubagentCard.tsx:5-7`），其余渲染逻辑不变。
- **一次 prompt 一张卡片**：`ga_send` 会开新的 `#<seq>`，旧卡片保持“已完成”，新卡片继续流。
  这样不会出现“卡片已完成但会话还能继续”的错位状态。
- `turns` 固定 0：卡片上的「N 个工具」已经由事件统计（`SubagentCard.tsx:67`），
  不要再叠加一个语义不同的「轮」。
- 消息持久化：block 类型仍是 `subagent`，刷新历史时由 `MessageList` 正常回放，
  不需要新的 DB 表或迁移。

### 8.3 文本与结果去重

**原设计这里判断错了，已修正。** GA 的 `agent_message_chunk` 是 `full_resp` 的增量
（turn banner + 工具进度 + 正文，GA `app.py:2048-2056`），而 bridge 在“已经流过 chunk”时
会丢弃 `done`（`bridge.py:425-467`）。所以：

- 拼接 chunk 得到的是完整 UI 流，不是干净的最终答复；
- `SubagentCard.tsx:60-66` 只比较“最后一个文本事件”，工具交错时不会命中，会重复展示。

**新规则**：

1. `session.py` 维护两份文本：
   - `full_text` = 所有 `agent_message_chunk` 的拼接（`ga_result` 传 `full=true` 时返回）；
   - `result` = **最后一个工具事件之后的文本**；若为空则退回 `full_text`。
2. `subagent_end` **不带 `result`**（有文本事件时），卡片只靠时间线展示，不再重复；
   无任何文本事件时才带 `result="（GA 未返回文本）"`。
3. `ga_result` 默认返回 `result`（干净后缀），需要全量时传 `full=true`。

---

## 9. 生命周期、并发与停止

### 9.1 作用域

- **一个 GA 进程 = 一个 GA 会话 = 一个 inner generation 内的一个 `ga_id`**。
- 会话随 generation 走：本轮结束统一 `close_all()`，不跨轮存活。
  - 理由：避免跨轮进程泄漏、避免“上一轮僵尸会话还在写 workspace”；
  - 代价：模型必须在同一轮里 `ga_wait` / `ga_result`，工具描述已明确要求；
  - 跨轮续跑（`session/load`）列为后续项目，本期非目标。
- **主轮先结束、GA 还在跑**：SSE 会继续挂着看它收尾，最多 `INNER_ACP_DRAIN_TIMEOUT_S`
  （默认 300s）；到点仍未结束则停 GA，并在卡片标 `stopped`。
  这段等待**不会**让模型再消费结果——模型必须在本轮里 `ga_wait` / `ga_result`。
  `has_active()` 的定义因此是「有 in-flight prompt 的会话」，空闲会话不算 active，
  否则每轮用过 GA 的对话都会在答完后多挂 5 分钟。

### 9.2 状态机

```text
starting → running → completed
                  ├→ errored   （进程退出 / 协议错误 / 超时）
                  └→ stopped   （ga_stop 或 /stop）
```

- `starting`：进程已起，未收到 `session/new` 响应；
- `running`：已发 `session/prompt`，prompt 未返回；
- 终态只允许进入一次；后续事件丢弃并记日志。

### 9.3 并发与配额

| 限制 | 默认 | 行为 |
|---|---|---|
| 单用户并发「进行中 prompt」 | 4 | 第 5 个 `ga_spawn` 等待 2s 后返回 `[工具执行错误] 并发上限` |
| 全局并发「进行中 prompt」 | 80 | 同上 |
| 单用户打开会话数（含空闲） | 8 | 超出时回收最久空闲的会话；仍超则报错 |
| 单会话同时 active prompt | 1 | 本地拒绝（G7），提示先 `ga_wait` |
| 会话墙钟上限 | 1800s | 到点 `cancel` → 2s → 杀进程，状态 `stopped` |
| 空闲上限 | 600s | 无 active prompt 且无工具调用则关闭（下轮会重建） |

**名额的定义**：并发名额按「有 in-flight prompt 的会话」计数，不是按「打开着的会话」计数。
`session/prompt` 发出时 acquire，prompt 返回（或取消/失败）时立即 release。
这样「先完成的任务不会一直占坑」，同一轮里可以先后派多个任务。

避免进程数量的第二道闸：单用户打开会话数上限 8（含空闲会话），配合 `IDLE_TTL_S` 回收。
配额实现：在 `quota.py` 新增 `user_acp_active` / `global_acp_active` 两组信号量，
沿用 `QuotaManager._acquire_many` 的等待语义（`quota.py:56-163`）。

### 9.4 停止级联（三层）

1. **模型主动**：`ga_stop` → `session/cancel` 通知 → 等 2s → 未返回则 `_kill_tree`。
2. **用户点停止**：`/stop` 的顺序固定为
   `hub.stop_subagent_generation` → **`acp_manager.stop_generation`（优雅，≤2s）** →
   `process_registry.stop_generation`（硬杀兜底）。
   - GA manager 按 `(user_id, session_id, generation_id)` 注册进 `RuntimeHub`（新增
     `register_acp_manager` / `get_acp_manager`，与 `_subagent_hosts` 同构），停止端点才能找到它。
   - 优雅路径：对每个会话发 `session/cancel`，等 prompt 响应最多 2s；返回则正常收尾，不杀进程。
   - 硬杀兜底：GA 进程**仍注册 `ProcessLease`**，`proc` 指向 `acp/transport.py` 里的
     `AsyncProcHandle`（暴露 `pid` / `returncode` / `kill()` / **异步 `wait()`**）。
     注意：`process_registry._kill_tree` 会 `await proc.wait()`（`process_registry.py:109-112`），
     所以**不能把裸 `subprocess.Popen` 塞进 `lease.proc`**，否则同步 `wait()` 会阻塞事件循环。
     优雅路径先跑，硬杀只会命中“2 秒内没退掉”的进程，行为符合预期。
3. **本轮结束**：`chat.py:155-205` 的清理块里 `await turn.ga.close_all()`。

### 9.5 进程退出与异常

| 情况 | 处理 |
|---|---|
| bridge 启动失败（模块不存在/解释器错误） | `ga_spawn` 返回可读错误；不注入半成品会话 |
| 读线程 EOF | 所有 pending 请求失败；发 `subagent_end{status:"errored",error}` |
| 协议行无法解析 | 记 stderr 环形缓冲，继续读 |
| `session/prompt` 回 `end_turn` 但无任何文本 | 视为完成，`result=""`，卡片提示「GA 未返回文本」 |
| stderr 出现 `[ACP-BRIDGE] WRITE FAILED` | 记录并在 `ga_result` 的 `error` 字段透出最近 20 行 |

---

## 10. 安全与隔离

### 10.1 工作目录

- `cwd` 由服务端拼：`user_workspace(user_id)`（`workspace.py`），**绝不接受模型输入**。
- GA bridge 用它构造 `GeneraticAgent(workspace_root=cwd)`（bridge `:193-200`），
  因此 GA 的文件工具被限制在该目录（GA `handler.py:1021-1045`、`:947-956`）。
- GA 的 `code_run` 在 `workspace_root` 下**只允许 Python**，shell/PowerShell 直接拒绝
  （GA `handler.py:84-86`），Python 走 workspace guard（`:145-150`）。
- **明确边界**：这不是 OS 沙箱。GA 进程仍以服务进程的账号运行；恶意 Python 仍可能
  通过 ctypes / 子进程绕过 guard。生产环境需要容器/独立账号（与本项目现有 `code_run`
  的定位一致，见 `run_guard.py:1-5`）。

### 10.2 环境变量

- 默认透传集合（**不是最小集合**，少一个都可能让 GA 起不来）：
  `PATH`、`SystemRoot`、`SystemDrive`、`windir`、`TEMP`、`TMP`、`USERPROFILE`、
  `HOMEDRIVE`、`HOMEPATH`、`APPDATA`、`LOCALAPPDATA`、`LANG`、`PYTHONIOENCODING`。
- **`APPDATA` / `LOCALAPPDATA` 是硬要求**：GA 安装版用 `%APPDATA%/GenericAgent` 找 `llm.yaml`
  （GA `genericagent/paths.py:110-126`），缺了它 `GeneraticAgent` 初始化失败、`session/new`
  返回 `-32603`，每次 `ga_spawn` 都失败。
- 代理变量 `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`（存在时）一并透传，否则 GA 的模型调用可能出不了网。
- 需要额外变量时用白名单 `INNER_ACP_ENV_ALLOW="FOO,BAR"` 显式声明。
- 需要额外变量时用白名单 `INNER_ACP_ENV_ALLOW="FOO,BAR"` 显式声明。
- **绝不**把 inner 的 `llm.yaml` key、`INNER_*` 密钥、数据库路径透给 GA；
  GA 用它自己的配置（§11.2）。

### 10.3 权限

- 默认 `full_access`，与现有子代理“有工具就直接用、不弹窗”一致；
- `permission` 自动应答仅作防死锁兜底，且每次都在卡片留痕；
- 不把 `ask` 暴露为默认，因为本服务没有权限对话 UI。

### 10.4 输入与输出

- 任务书来自模型，信任级别等同子代理的 `spawn_agent.message`；
- GA 输出进入 `result` 前做一次长度截断（默认 20000 字符，与 `read_agent_result` 对齐）；
- GA bridge 已对 `rawInput` / `content` 做敏感信息脱敏（`bounded_text` + `redact_sensitive_text`，
  bridge `:76-84`），本项目直接沿用，不重复实现。

---

## 11. 配置

### 11.1 环境变量（全部可选，缺省即关闭）

| 变量 | 默认 | 说明 |
|---|---|---|
| `INNER_ACP_GA_COMMAND` | 空 | 完整命令行。为空时**不注入 GA 工具**，系统提示与现在完全一致 |
| `INNER_ACP_GA_PYTHON` | 空 | 只给解释器时，自动拼 `-m frontends.acp.genericagent_acp_bridge` |
| `INNER_ACP_GA_LLM_NO` | 空 | 透传给 bridge 的 `--llm-no`，选择 GA 侧 LLM 配置序号 |
| `INNER_ACP_MODE` | `full_access` | 新建会话后应用的 ACP mode |
| `INNER_ACP_PERMISSION` | `allow` | 收到 `session/request_permission` 时的自动应答：`allow` / `deny` |
| `INNER_ACP_WORKDIR_MODE` | `workspace` | `workspace`=用户 workspace 根；`subdir`=`.ga/<generation_id>/` 子目录 |
| `INNER_ACP_MAX_ACTIVE_USER` | 4 | 单用户并发「进行中 prompt」 |
| `INNER_ACP_MAX_ACTIVE_GLOBAL` | 80 | 全局并发「进行中 prompt」 |
| `INNER_ACP_MAX_OPEN_USER` | 8 | 单用户打开会话数（含空闲）硬上限 |
| `INNER_ACP_PROMPT_TIMEOUT_S` | 1800 | 单次 prompt 墙钟上限 |
| `INNER_ACP_DRAIN_TIMEOUT_S` | 300 | 主轮结束后等待 GA 收尾的上限，超时停 GA |
| `INNER_ACP_IDLE_TTL_S` | 600 | 空闲回收 |
| `INNER_ACP_STDERR_LINES` | 200 | stderr 环形缓冲行数 |
| `INNER_ACP_ENV_ALLOW` | 空 | 额外透传给 GA 的环境变量名，逗号分隔 |

命令示例（本机）：

```text
INNER_ACP_GA_PYTHON=C:\anaconda3\python.exe
INNER_ACP_GA_LLM_NO=0
```

> 注意：GA 的 bridge 必须能从该解释器 import。若跑的是源码 checkout，
> GA 自己的 demo 要求 `--allow-source`；本项目不做这个特判——**由运维保证 GA 已正确安装**。
> 启动失败的诊断信息取 stderr 环形缓冲最后 20 行。

### 11.2 GA 的模型

按已确认的决策：**GA 用自己那套 API 配置**（GA 的 `active_profile` / `--llm-no`），
inner_chatgpt 不注入自己的模型 key，也不把 inner 的 `llm.yaml` 传给 GA。
这意味着同一个任务可能出现两套模型计费，属于已接受的成本。

### 11.3 启动预检（首次 `ga_spawn` 时执行一次并缓存）

```text
<python> -c "import frontends.acp.genericagent_acp_bridge as b; print(b.__file__)"
```

- 成功：拿到 bridge 绝对路径，算 SHA256，写日志 + 会话元数据（用于 R1 的版本锚定）；
- 失败：返回 `[工具执行错误] GenericAgent ACP bridge 不可用：<stderr 末 20 行>`，
  并**不注册**该工具集（避免模型反复调用一个必然失败的工具）。
- 预检结果按“解释器路径 + 命令”缓存，进程重启才重算。

---

## 12. 实施步骤（建议按阶段提交）

### Phase 0：传输与协议（不接真 GA）

1. `transport.py`：Popen、读写线程、行解析、关闭；单测用 `sys.executable` 跑假 bridge。
2. `client.py`：请求关联、通知分发、权限自动应答、EOF 失败传播。
3. `tests/fixtures/fake_ga_bridge.py`：按环境变量模拟
   `chunk / tool_call / permission / crash / hang / malformed_line` 六种行为。
4. 验收：单测覆盖 §13.1 全部条目，`pytest backend/tests/test_acp_client.py` 全绿。

### Phase 1：最小闭环（spawn / wait / result）

1. `session.py` + `manager.py`，实现建会话、发 prompt、收事件、落结果。
2. 事件映射（§8.1）与 `SubagentCard` 联调。
3. 挂载：`subagent/session.py` + `chat.py` 的 `has_active` 与 `close_all`。
4. 验收：假 bridge 下，前端出现 GA 卡片，文本流式、工具行出现、结果落卡片。

### Phase 2：追问、停止与配额

1. `ga_send` / `ga_stop`；`session/cancel` 的 2s 宽限与强杀。
2. `/stop` 级联：在 `chat.py` 停止块里追加 `acp_manager.stop_generation(...)`。
3. `quota.py` 两组信号量 + 空闲 GC + 墙钟超时。
4. 前端 `ROLE_LABEL.ga`。
5. 验收：`/stop` 后 3s 内进程消失（`tasklist` 复核）；第 3 个 spawn 被配额拒绝。

### Phase 3：真实 GA E2E 与文档

1. 真实 GA 跑三类任务：
   - 写文件型：在 workspace 创建 `acp_ga_e2e.txt`，内容含哨兵 token；
   - 多步型：读文件 → 改一行 → 跑 `python -m pytest -q` 式校验；
   - 长任务型：故意 sleep 30s，验证 `ga_wait` 超时返回 running 且不杀死。
2. 记录 GA `agentInfo.version` 与 bridge SHA256 到 `docs/` 的验收记录。
3. 更新 `README.md` 能力描述与 `AGENTS.md` §0.1 的 P5 状态；
   新增 `docs/20261109-ACP-client驱动GenericAgent实施记录.md`（实施完成后写）。

> 每个 Phase 单独可验证、可回滚；未完成的 Phase 不写进 README / AGENTS 的能力描述。

---

## 13. 测试与验收

### 13.1 单测（假 bridge，不依赖真 GA）

| 用例 | 断言 |
|---|---|
| 握手 | `initialize` 发出版本 1，收到 `agentInfo`，写入会话元数据 |
| 行框架 | 两个 JSON 粘在一行 / 一行拆成两次写入 → 解析器不丢不乱 |
| 请求关联 | 并发 3 个请求（不同 id）各自 resolve，不串号 |
| 权限自动应答 | 假 bridge 发 `session/request_permission` → 客户端回 `allow_once`；prompt 继续 |
| 权限 deny 配置 | 回 `reject_once`，且卡片出现权限事件 |
| 并发 prompt | 本地拒绝，不写给 bridge |
| 进程崩溃 | 读线程 EOF → pending future 全失败 → `subagent_end{errored}` |
| 协议垃圾行 | 不中断读循环，stderr 缓冲有记录 |
| 停止 | `ga_stop` → 收到 `session/cancel`；2s 未返回 → 进程被杀 |
| 配额 | 第 3 个 spawn 等待 2s 后返回可读错误 |
| 超时 | prompt 超过 `PROMPT_TIMEOUT_S` → cancel + kill，状态 `stopped` |

### 13.2 集成（真 GA）

哨兵 token 规则（避免“没报错就算过”的假验收）：任务书里带本次唯一的
`ACP_GA_OK_<随机8位>`，要求 GA 把它写进文件并复述；测试断言文件内容含该 token。
反向确认：不配置 `INNER_ACP_GA_COMMAND` 时，工具列表里**没有** `ga_*`，
模型无法调用——证明注入是有条件的。

### 13.3 回归（能力不退化）

- 现有 `backend/tests/` 全量通过（尤其 `test_subagent_*`、`test_team_*`、`test_kernel.py`）；
- `_merged_events` 改动后，纯子代理场景的 SSE 事件序列与改动前一致（对比事件名/顺序）；
- 不配置 GA 时，`/api/mcp/tools` 返回的工具列表与改动前逐项相同。

### 13.4 验收标准（DoD）

1. 主 agent 能 `ga_spawn` 一个编码任务，主对话不卡；
2. 卡片中能看到 GA 的文本与工具步骤；
3. `ga_result` 返回的产物在 workspace 中可被 `file_read` 验证；
4. `/stop` 3 秒内 GA 进程消失；
5. `tasklist` 在测试结束后无残留 `python -m frontends.acp...`；
6. 无 GA 配置时系统行为与当前完全一致。

---

## 14. 风险与未决项

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | GA 版本漂移，bridge 协议/行为变化 | 高 | `agentInfo.version` 是 bridge 硬编码的 `0.1.0`（不能反映真实版本）；改为**启动预检**：用配置的解释器 import bridge 并打印 `__file__`，对该文件算 SHA256，写入日志与会话元数据；握手失败即明确报错，不做静默降级 |
| R2 | GA 内部若进入“询问用户”的分支（plan/ask），ACP 没有对应通道 | 中 | 默认 `full_access`；用墙钟超时兜底；Phase 3 专门测一次 `ask` 模式的实际表现 |
| R3 | GA 自己的 subagent/team 事件不透传（bridge 只发顶层 `session/update`） | 中 | 明确为已知限制：卡片只展示 GA 顶层工具调用与文本 |
| R4 | Windows 管道编码/换行 | 中 | 全程二进制 + 显式 UTF-8；`\r` 剥离；单测覆盖粘包/拆包 |
| R5 | `_merged_events` 的 `has_active` 改动引入主 SSE 不退出 | 中 | 仅 OR 条件；超时/停止路径保持原逻辑；回归对比事件序列 |
| R6 | 两套模型计费与延迟 | 低 | 工具描述引导“小任务别用 GA”；配额限制并发 |
| R7 | GA 的 stdout 被自身 `print` 污染 | 低 | bridge 已 dup2 到 stderr（`:14-45`）；客户端仍按“非 JSON 行丢弃 + 记缓冲”处理 |
| R8 | 长任务中途本轮结束，GA 被关闭 | 中 | 工具描述强制“派活后必须 wait”；卡片状态明确写 `stopped`，不谎报完成 |

### 14.1 已确认决策（2026-11-09，用户拍板）

| # | 决策 |
|---|---|
| 1 | **工作目录 = 用户 workspace 根**（GA 直接改用户文件，产物不用搬运） |
| 2 | **并发 = 4（单用户）/ 80（全局）**，限制的是「同时进行中的 GA prompt」，不是工具调用次数 |
| 3 | **只有 root agent 能派 GA**；teammate / 子代理不注入 `ga_*` 工具 |
| 4 | **本期只支持一套全局 GA 配置**（`INNER_ACP_GA_*`），不做按用户区分 |

### 14.2 reviewer 提出、由实现者自行决定并已写入本设计的点

1. **环境白名单**：必须透传 `APPDATA` / `LOCALAPPDATA`，否则安装版 GA 找不到 `llm.yaml`
   （GA `paths.py:110-126`），`session/new` 必失败（§10.2）。
2. **停止顺序**：优雅 `session/cancel`（≤2s）在前，`process_registry` 硬杀在后；
   `ProcessLease.proc` 必须是带异步 `wait()` 的 `AsyncProcHandle`，不能塞裸 `Popen`（§9.4）。
3. **`has_active()`**：只算「有 in-flight prompt」的会话；空闲会话不吊住 SSE。
   主轮先结束时最多再等 `INNER_ACP_DRAIN_TIMEOUT_S=300`，到点停 GA（§9.1）。
4. **结果文本**：`full_text` 与 `result`（最后工具事件之后的后缀）分开，
   `subagent_end` 有文本时不带 `result`，避免卡片重复（§8.3）。
5. **取消安全清理**：manager 提供同步 `stop_all_now()`，挂进 `_cleanup_turn` 的同步段与
   `_settle_turn` 兜底，保证断连/取消也不漏进程（§9.4、§5.4）。
6. **配额口径**：按 in-flight prompt 计数（完成即释放），另加“打开会话数 8”防进程膨胀（§9.3）。
7. **GA 自己的 MCP 服务器**：默认沿用 GA 的 `GA_MCP_CONFIG` / `mcp.json`；
   如需减小进程树，可设 `INNER_ACP_GA_MCP_CONFIG` 指向空配置。
   `/stop` 的 `taskkill /T /F` 会连同这些子进程一起收掉。
8. **权限事件**：自动应答必须发 `tool_start` + 配对的 `tool_result`，否则卡片永远转圈（§6.5）。

---

## 15. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-09 | 初稿：选定 B 路线（后台 ACP 会话 + 5 工具 + 复用 SubagentCard）；明确 GA 用自身模型配置；给出协议契约、事件映射、生命周期、安全边界、分阶段实施与验收 |
| 2026-11-09 | 按用户在四个决策点上的拍板更新（workspace 根 / 4+80 / root-only / 单套全局配置）；吸收独立 reviewer 的 11 条意见：环境白名单、停止顺序与异步 `wait` 适配、`has_active` 与 drain、结果文本两分法与去重、取消安全清理、配额口径、GA MCP 进程树、权限事件配对、版本锚定改 SHA256 预检 |
