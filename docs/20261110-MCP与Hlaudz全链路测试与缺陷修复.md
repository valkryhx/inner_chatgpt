# MCP × Hlaudz 全链路测试与缺陷修复记录

> 日期：2026-11-10　状态：已完成（含 3 个真实缺陷修复）　分支：`main`
> 被测提交：`b44ea3f`（工具来源可见 + 连接状态可见 + 软 TTL 刷新）
> LLM：真实湛卢 API（`moma/deepseek-latest-flash`，`https://ecloud.10086.cn/...`）
> MCP：真实 `sqlbotmcp`（`http://127.0.0.1:8765/mcp`，12 个工具）

## 0. 一句话结论

提交 `b44ea3f` 的三个目标（工具来源可见、状态可见、软 TTL）**在正常路径上全部成立**：
真实湛卢对话里模型能正确说出 sqlbot 的工具清单、取数结果与库内真实值一致、
断链时如实报告而不是编造、前端侧栏与设置页如实显示红绿状态。

但真实压测同时抓出 **3 个单测原理上测不到的链路缺陷**，其中 1 个会让用户永久卡死：

| # | 缺陷 | 严重度 | 现象 | 状态 |
|---|---|---|---|---|
| 1 | MCP 会话僵死不自愈 | **高（永久卡死）** | server 重启后缓存 client 报 `Session terminated`，TTL 判「状态没变」不重建，用户每轮调用都失败，只能重启后端 | 已修复 |
| 2 | 跨 server 同名工具冲突 | 中（静默调错） | 两个 server 都有 `health` 时 schema 里出现重名 function，`dispatch` 永远命中最后注册的，且模型分不清 | 已修复 |
| 3 | MCP 结果没有预算 | 中（撑爆上下文） | `run_readonly_sql` 一次返回 115 KB 原样进 history；内置工具有预算，MCP 没有 | 已修复 |

另修 1 处契约不一致：`/api/mcp/tools` 返回原始 description，丢了 `[MCP: <server>]` 来源前缀，
与模型实际看到的版本不一致（用户按此判断归属会被误导）。

## 1. 测试方法（三层，全部真实环境）

| 层 | 手段 | 覆盖 |
|---|---|---|
| L1 单测 | `pytest backend/tests` | 488 项全绿（原 451 + 本次新增 37） |
| L2 真实后端 + 真实 MCP | httpx 直连 `POST /api/sessions/{id}/messages`，SSE 逐条落盘 | 接口契约、热生效、TTL、断链、子代理 |
| L3 真实浏览器 | Playwright（Chromium）跑真实 UI | 侧栏状态条、设置页汇总、重检按钮、红绿状态 |

三条硬约束（依 `.claude/skills/hlaudz-e2e/SKILL.md`）：

1. **真实 LLM**：全部走湛卢 API，无假 LLM；
2. **唯一 runId + 事件落盘**：每个场景的 SSE 事件写到 `work/mcp_*/<runId>/`；
3. **哨兵 token**：子代理场景塞随机串要求复述，不复述即视为未送达。

所有断言基于落盘事件与持久化消息，不读 stdout 猜测。

## 2. 测试结果总览

| 套件 | 场景 | 断言数 | 结果 | 现场 |
|---|---|---|---|---|
| A 来源与状态 | 工具来源前缀、effective、probe | 13 | **全绿** | `work/mcp_e2e/20261110-232218` |
| B 真实对话 | 来源识别、单点取数 ×3、SQL 逃生口、负对照 | 21 | **全绿** | `work/mcp_e2e/20261110-235510-45b0` |
| C 断链/TTL/子代理 | 断链如实告知、TTL 自动发现、子代理继承 | 12 | **全绿** | `work/mcp_e2e2/20261110-233023-607b` |
| D 前端 Playwright | 侧栏状态条、设置页汇总、重检、断链红点 | 14 | **全绿** | `work/mcp_ui/20261110-235649-36a1` |
| E 缺陷复现（修复前） | 会话僵死 | 9 | **2 失败** → 定位缺陷 1 | `work/mcp_stale/20261110-233628` |
| F 修复后复验 | 重启自愈、同名工具、大结果 | 13 | **全绿** | `work/mcp_fixed/20261110-235304` |
| G TTL 自愈复验 | 不点重检，仅靠 TTL 恢复 | 5 | **全绿** | `work/mcp_ttl_fix/<runId>` |

## 3. 关键实测证据

### 3.1 工具来源可见（目标 1）

真实湛卢对话里问「你挂载了哪些 MCP server」：

```
## 已连接的 MCP server
**sqlbotmcp 提供的工具：共 12 个**
| health | metric_search | metric_list | zone_list | time_options |
| dimensions | table_schema | metric_series | metric_latest | metric_compare |
| chart_spec | run_readonly_sql |
```

模型给出的清单与 `/api/me/mcp-effective` 返回的 12 个工具**逐字一致**，且**没有**用
`code_run` 手搓协议去探测（`forbid_tools` 断言通过）。这正是 `b44ea3f` 想解决的问题：
此前 hx 会话里模型实际调用了 `metric_search` 却声称「没有 sqlbot 工具」。

模型实际收到的 schema（`ToolRegistry.schemas()`）：

```json
{"name": "health", "description": "[MCP: sqlbotmcp] 健康检查：DB 连通性、库类型、三张表列名。"}
{"name": "metric_search", "description": "[MCP: sqlbotmcp] 按中文名模糊搜索指标…"}
```

12 个工具**全部**带前缀，内置工具（`file_read` 等）不带。

### 3.2 sqlbot 调用成功率（用户最关心）

同一问题「全国 2026 年 7 月家庭宽带用户数」连跑 3 次：

| 轮次 | 工具序列 | 数值 | 结束状态 |
|---|---|---|---|
| C2-0 | `metric_search → time_options → metric_latest → metric_compare → code_run` | 114,124,518 | complete |
| C2-1 | `metric_search → time_options → metric_latest → metric_series → code_run` | 114,124,518 | complete |
| C2-2 | `metric_search → time_options → metric_latest → run_readonly_sql → table_schema → metric_series` | 114,124,518 | complete |

**3/3 全部成功**，数值与库内真实值完全一致，且三次走了三条不同的工具路径
（模板优先、模板+核算、SQL 交叉核对），说明模型是按 manifest 自主选择而不是碰运气。

多步场景（TOP5 省份 + 环比）走 `metric_search → time_options → table_schema →
run_readonly_sql ×4`，结果 `内蒙古 / 河北 / 西藏` 与独立 SQL 复核一致。

**值得记的一点**：C2-0 里模型主动指出 `metric_compare` 的 delta 符号方向有问题
（绝对值对、符号不可信），自己用两个月原始值重算。这是「工具返回值可疑时不自欺」的正确行为。

### 3.3 断链如实告知（目标 2 的负向验证）

把 sqlbot 指向不可达端口后问「你能用 sqlbot 查家庭宽带用户数吗」：

> 失败详情：`RuntimeError: Client failed to connect: All connection attempts failed`，
> 所有连接尝试都未成功，连会话都没有建立，因此无法产生任何查询。
> 我这边没有任何可替代的数据库通道…

- 未编造任何数字（`114124518` 断言不存在）；
- 明确说明失败原因，而不是「本来就没有这个 server」；
- 明确拒绝用 `code_run` 绕过。

### 3.4 前端状态可见（目标 2 的正向验证）

Playwright 实测（1440×900，真实登录）：

| 操作 | 侧栏 | 设置页 |
|---|---|---|
| sqlbot 正常 | `MCP 正常 · 20 工具`（绿点） | `全部 1 个 server 已连接` / `已连接 · 12 个工具` |
| 点「重新检测连接」 | — | `检测完成：全部已连接` |
| 改成不可达端口 → 重检 | — | `连接失败：sqlbotmcp（RuntimeError: Client failed to connect: All connection attempts failed）` |
| 刷新页面 | `1 个 MCP 断开`（红点） | — |
| 恢复 URL 后刷新 | `MCP 正常 · 20 工具`（绿点） | — |

浏览器控制台 **无 `pageerror`**。

### 3.5 软 TTL 自动刷新（目标 3）

绕过 API 直接改库（不触发 `invalidate`），验证 TTL 自己发现变化：

| 时刻 | 观测 | 结论 |
|---|---|---|
| TTL 内 | 仍返回缓存「已连接」 | 预期行为，不无谓重连 |
| TTL 过期（60 s） | `sqlbotmcp: ok=False`，工具数 0 | 自动发现断开 |
| 恢复 URL 后 60 s | 回到 `ok=True`，12 工具 | 自动恢复 |

## 4. 抓出的缺陷与修复

### 4.1 缺陷 1：MCP 会话僵死，用户永久卡死（高）

**复现**（`work/_mcp_stale.py`，真实后端 + 真实湛卢 + 临时 MCP server）：

```
T1 首次调用 → "pong from boot-A"          ✅
T2 杀掉 MCP server 进程并重启为 boot-B   （模拟服务重启/换实例）
T3 等 TTL 过期后读状态 → ok=True, 2 tools  ❌ 假活
T4 再调用 → [工具执行错误] McpError: Session terminated
T5 点「重新检测」→ probe 用新连接返回 ok=True
T5b 再调用 → 仍然 Session terminated
```

**根因**：两条独立机制都只认「能连上」，不认「这条连接还活着」。

1. `fastmcp.Client` 在 server 重启后**不会自己重连**，缓存里的旧 client 永久报
   `Session terminated`；
2. 软 TTL 的探测（`probe_mcp_status` → `build_user_toolset`）用的是**新建**连接，
   拿到的 manifest 是 `ok=True, 12 tools`，与缓存 manifest **完全一致** →
   `_same_manifest()` 判定「状态没变」→ 只续期，**不重建**。

于是坏 client 永远留在 `RuntimeHub` 缓存里。**用户点「重新检测」也没用** —— probe
只证明 `server` 活着，不证明 `缓存里的连接` 活着。唯一出路是重启后端。

这条与提交要做的「自动 TTL 重连」正面冲突：TTL 会刷新状态显示，但修不好连接。

**修复**（三层，互为兜底）：

1. `mcp_client.MCPToolset.reconnect()`：重建底层 client 并重新发现工具；
2. `MCPToolset._wrap()`：工具调用命中**传输类**错误（`session terminated` /
   `connection closed` / `ConnectError` …）时，自动重连并重试一次；
   业务类错误（参数校验、工具自身异常）**不重连**（重连也没用）；
3. `runtime._toolset_is_live()`：TTL 刷新时先对**缓存连接**做一次真实往返探活，
   死了就重建 runtime，再走原来的 manifest 比较。

探针设计的三条保守约束（避免误判与副作用）：

- 只挑**无必填参数**的工具（缺参会被服务端拒绝，误判为死）；
- 只挑名字属于健康/列举类的工具（`health` / `ping` / `list` / `options` /
  `dimensions` …），**绝不拿任意无参工具当探针** —— 有些 server 的首个无参工具
  可能是会写数据的动作；
- 找不到合适探针就**视为活**（宁可放过，不能错杀）。

**复验**（`work/mcp_fixed/20261110-235304`）：

```
D1 first  → "pong from A1"                     ✅
D1 重启为 A2 后立即调用 → "pong from A2"        ✅（修复前永久失败）
D1 不再出现「需要重启服务 / 刷新页面重建」      ✅
```

### 4.2 缺陷 2：跨 server 同名工具冲突（中）

两个 server 都暴露 `health` 时：

```
registry names() count: 3          ← 唯一键静默吞掉一个
schemas 发给模型的: 4 个，其中两个叫 "health"
dispatch('health') → 命中最后注册的 server
```

模型看到两个同名 function 分不清，dispatch 又永远路由到后者 —— **静默调错 server**。
真实网关对重复函数名返回 200（不报错），所以这个问题完全无声。

**修复**：`MCPToolset.merge()` 遇到重名时，首个保留原名，后续加 server 前缀
（`othermcp_health`），并把改名记录进 `_renamed`，由 `format_mcp_manifest()` 在系统
提示词里如实披露（模型才不会把 `tmpsrv2_ping` 当成陌生工具）。

**复验**：

```
工具名: [... 'ping', 'big', 'tmpsrv2_ping', 'tmpsrv2_big']   无重复 ✅
tmpsrv2_ping 的描述: "[MCP: tmpsrv2] 健康检查：返回本进程的启动标识。"  ✅
模型分别调用两个 server 的 ping，两次返回都拿到（A2 / B1）    ✅
```

### 4.3 缺陷 3：MCP 结果没有预算（中）

实测同一条 `run_readonly_sql`：

```
预算层修复前：115,685 字符   直接进 history
预算层修复后：  4,160 字符   + 落盘路径
```

内置工具（`file_read` / `code_run` / `file_grep` …）有 per-tool 上限、超限落盘、
头尾预览，**MCP 工具此前完全没有**。一次查 1000 行就吃掉大半上下文预算，
紧接着触发压缩、甚至截断真正的回答（"输出被截断"在真实会话里非常容易被误读成模型不行）。

**修复**：MCP 结果统一走 `tool_budget.apply_budget`，工具名记为 `mcp:<server>:<tool>`，
超限时落盘到 `<workspace>/.hlaudz/tool_results/` 并给出可读路径：

```
… [111,685 chars omitted; full output: .../tool_results/43ad7313.txt (read with file_read)] …
```

**复验**：`rows=3000` 的请求，模型仍能正确回答「总行数 3000 行」，原始数据没有进上下文。

### 4.4 契约不一致：`/api/mcp/tools` 丢来源前缀

端点返回 `t.description`（原始），而模型看到的是 `describe_for_model()`（带前缀）。
同一份工具在两个地方长得不一样，用户按此判断归属会被误导。

**修复**：返回 `description`（模型视角，带前缀）+ `raw_description` + `source` 三个字段。

> 注：前端目前**未消费**该端点（`api.ts` 有定义、无调用方），所以这条不影响线上 UI，
> 但契约必须对齐 —— 否则将来做「工具面板」时会把这个问题带到界面上。

## 5. 变更文件

| 文件 | 改动 |
|---|---|
| `backend/app/mcp_client.py` | 会话失效判定、`reconnect()`、调用级自动重连重试、`merge()` 同名去重、MCP 结果预算 |
| `backend/app/runtime.py` | `_toolset_is_live()` 活性探活、`refresh_mcp_status()` 先探活再比 manifest |
| `backend/app/agent.py` | `format_mcp_manifest()` 披露改名 + 告知模型「传输错误会自动重连」 |
| `backend/app/routers/chat.py` | `/api/mcp/tools` 返回模型视角描述 + `source` |
| `backend/tests/test_mcp_session_resilience.py` | 新增 22 项（重连、同名、预算） |
| `backend/tests/test_mcp_session_liveness.py` | 新增 13 项（活性判定、探针选择、refresh 集成） |
| `backend/tests/test_mcp_tools_endpoint.py` | 新增 2 项（端点来源契约） |

## 6. 反向确认（验证测试真的在测东西）

每项修复都做了「去掉实现 → 测试必须变红」：

| 去掉什么 | 转红 |
|---|---|
| 调用级自动重连 | 2 项 |
| `merge()` 同名去重 + 结果预算 | 5 项 |
| TTL 活性探活（`live = True`） | 1 项（`test_refresh_rebuilds_when_cached_connection_is_dead`） |

恢复后全绿，说明断言确实锚在修复上，不是同义反复。

## 7. 已知限制与未覆盖

1. **快照式探针**：`_toolset_is_live` 的探针优先级是「无参 + 名字像健康类」。
   若某 server 一个这样的工具都没有（例如只有 `run_sql`），探活会跳过并**视为活**，
   此时仍可能残留僵死 client —— 不过调用级的自动重连仍会兜底（用户侧无感）。
2. **重连的窗口期**：重连期间该次调用会多花一次连接时间（实测 sqlbot 约 40–400 ms）。
   并发调用同一 server 时可能同时触发多次重连；结果正确，只是有重复建连开销。
3. **exa / tavily 等公网 MCP** 本身不稳定（实测多次 `TimeoutError`）。
   这属于上游可用性问题；现在的行为是**如实显示失败原因**，而不是假装没有这个 server。
4. **未做**：MCP 工具级超时（目前只有连接超时 `MCP_CONNECT_TIMEOUT=12s`）；
   `USER_MAX_MCP_CALLS=2` 在模型一轮并发调 3 个以上 MCP 工具时会触发 2 s 等待，
   超时后该次调用返回可读错误（模型会重试），但体验上仍有优化空间。

## 8. 复现命令

```bash
# 后端
cd backend && python run.py

# 后端真实 LLM e2e（需 sqlbot 在 8765）
python work/_mcp_llm_e2e.py        # 来源识别 + 取数成功率 + 负对照
python work/_mcp_e2e2.py           # 断链如实告知 + TTL + 子代理
python work/_mcp_fixed_e2e.py      # 重启自愈 + 同名工具 + 大结果预算

# 前端（需后端已服务 frontend/dist）
python work/_mcp_ui_e2e.py

# 单测
cd backend && python -m pytest tests -q
```

## 9. 变更记录

- 2026-11-10：首次全链路测试（提交 `b44ea3f`），抓出 3 个链路缺陷 + 1 处契约不一致，
  全部修复并复验；单测 451 → 488。
