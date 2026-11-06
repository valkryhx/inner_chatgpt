# sqlbot_mcp 接入 jiakuan 与 E2E 验证

> 记录把外部 HTTP MCP（`sqlbot_mcp`：家客指标查表）接入本平台，并按用户隔离配置、
> 跑通对话端到端的过程与证据。更新：2026-11-04。

## 一、目标与结论

- 目标：`jiakuan_langchain` 的 LangGraph Agent 通过标准 **HTTP MCP** 调用 `sqlbot_mcp`，
  由 Agent 自主决定查哪些指标/区域/时间，实现「自然语言问家客指标 → 自动查表 → 作答」。
- 结论：**已跑通**。模板路径（`metric_series` 等）与逃生口路径（`run_readonly_sql`）均 E2E 通过。
- 配置按用户持久化，重启不丢；**不删除用户既有 MCP**（增量 upsert）。

## 二、配置方式（按用户，可在前端「配置」操作）

- 服务：`sqlbot_mcp` HTTP MCP 已运行于 `http://127.0.0.1:8765/mcp`。
- test 用户（`123@qwe.com`）在 MCP 配置里粘贴（与既有 exa 并存、互不影响）：

```json
{"mcpServers":{"sqlbot":{"type":"http","url":"http://127.0.0.1:8765/mcp"}}}
```

- 后端接口：`POST /api/me/mcp-servers/import`（`merge_mcp_json` 按 name upsert，不清空）。
- 验证：test 用户现有 `exa`（原样保留）+ `sqlbot`（新增）两条；重启后仍在。

## 三、端到端验证（真实 SSE，test 用户）

### 3.1 模板路径

问题：「全国近半年家庭宽带用户数的变化趋势？」

Agent 自动工具链：`metric_search(家庭宽带用户)` → `time_options()` →
`metric_series(metric_ids=[1], zone_ids=[99], gran=2, 2026-01~2026-12)`。

结果：正确返回 2026-02~07 六个月数据，换算成「万户」并给出环比表与要点
（半年净增约 90.98 万户、+0.80%，5 月单月净增 44.6 万户等）。

### 3.2 逃生口路径

问题：「2026年7月各省份家庭宽带用户数相比6月的环比增速，按增速降序 TOP5，一次 SQL 算完。」

Agent 自动工具链：`metric_search` → `table_schema()`（先取真实列名）→ `time_options()` →
`run_readonly_sql`（先探数据分布）→ `run_readonly_sql`（最终自连接 + `NULLIF` 防除零的环比增速 SQL）。

结果：正确给出省级 TOP5（安徽 +5.64%、四川 +2.85%…），并在回答中回显所用 SQL。
证明「模板之外靠 LLM 写只读 SQL」的逃生口可用，且 `sql_guard` 校验、自动 LIMIT 正常。

## 四、重要修复：MCP 连接缺少单服务器超时

- 现象：接入后首次调用 MCP 工具聚合接口 `/api/mcp/tools` 可能长时间挂起（>60s）。
- 根因：`build_user_toolset` 逐个 `await MCPToolset(...).__aenter__()`，对**单个服务器无超时**；
  一个慢/无响应的 MCP（如外网 exa，实测约 10s，网络差时更久）会拖住整个用户运行时，
  甚至持锁导致后续请求排队。
- 修复：`backend/app/agent.py` 的 `build_user_toolset` 对每个服务器连接加
  `asyncio.wait_for(..., timeout=MCP_CONNECT_TIMEOUT)`（默认 12s），超时/失败即清理并降级跳过，
  不影响其它服务器。修复后：test 用户聚合 11 个 sqlbot 工具正常，缓存后约 6ms。
- 相关环境变量：`MCP_CONNECT_TIMEOUT`（秒，默认 12）。
