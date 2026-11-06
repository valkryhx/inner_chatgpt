# P1 changelog：Skill 模块 + 基础工具（read / write / patch / runcode / load_skill）

> 日期：2026-11-05
> 前置：`docs/20261105-changelog-P0.md`（去 LangChain / LangGraph 已完成）
> 设计：`docs/20261105-Skill与基础工具设计与实施.md`

## 1. 新增能力

### 1.1 内置工具（Agent 可直接调用）

| 工具 | 说明 |
|---|---|
| `file_read` | 读文本文件，支持行范围 / 关键字定位 / 行号 |
| `file_write` | 写文件，overwrite / append / prepend |
| `file_patch` | 唯一匹配替换（0 或多处都报错） |
| `list_files` | 列 workspace 或可见目录，有深度/数量上限 |
| `code_run` | 跑 Python 脚本或 PowerShell 命令，超时 + 输出截断 |
| `load_skill` | 按名字加载 SKILL.md 全文（渐进披露） |

**权限边界（MVP，非强沙箱）：**

- 读：个人 workspace + 当前用户可见的 Skill 目录；
- 写/改：仅个人 workspace（`backend/data/workspaces/<user_id>/`）；
- 所有路径 `resolve()` 后校验包含关系，拒绝 `..` / 绝对路径 / 符号链接逃逸；
- `code_run`：cwd 固定 workspace，默认超时 60s（最大 600s），stdin 关闭，输出截断 20000 字符。

### 1.2 Skill 模块

- 存储：文件系统 `SKILL.md`，双轨
  - 内置：`backend/skills_builtin/<name>/SKILL.md`（只读，所有用户可见）
  - 私有：`backend/data/skills/<user_id>/<name>/SKILL.md`（仅本人可写）
  - 同名私有覆盖内置。
- 渐进披露：
  - 系统提示注入 **L1 清单**（`[Available Skills]` + 名称/描述，8000 字符预算）；
  - 正文由 `load_skill` 工具按需加载（**L2**），不占用每轮 context。
- 占位符替换：`${SKILL_DIR}` / `${CLAUDE_SKILL_DIR}` / `${GA_SKILL_DIR}` / `$ARGUMENTS`。
- 内置示例 Skill：`data-analysis`（数据分析工作流）。

### 1.3 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/skills` | 内置 Skill（只读） |
| GET | `/api/me/skills` | 当前用户可见 Skill（内置 + 私有） |
| GET | `/api/me/skills/{name}` | 单个 Skill 详情（含正文，用于编辑） |
| POST | `/api/me/skills` | 新建私有 Skill |
| PATCH | `/api/me/skills/{name}` | 修改私有 Skill（可改名） |
| DELETE | `/api/me/skills/{name}` | 删除私有 Skill（内置返回 405） |

写操作后 `hub.invalidate(user_id)`，下次请求重建 runtime、刷新 L1 清单。

### 1.4 前端

`设置` 弹窗新增「我的 Skills」tab：

- 列出内置 + 私有 Skill（来源标签、描述、字符数）；
- 新建 / 编辑（含正文回填）/ 删除私有 Skill；
- 内置 Skill 只读。

### 1.5 工具清单接口

- `GET /api/mcp/tools` 与 `GET /api/me/mcp-effective` 现在返回**内置 + MCP** 的完整工具集
  （之前只返回 MCP）。

## 2. 代码结构

```
backend/app/
  workspace.py       # 用户 workspace / skill 目录 + 路径安全（safe_join）
  skill_store.py     # SKILL.md 解析 / 发现 / 加载 / 用户私有 CRUD / L1 清单
  builtin_tools.py   # file_read / file_write / file_patch / list_files / code_run / load_skill
  routers/skills.py  # Skill HTTP API
backend/skills_builtin/data-analysis/SKILL.md  # 内置示例
backend/tests/test_skills_tools.py             # 11 项单测
```

改动文件：`config.py`（新增目录常量）、`agent.py`（合并工具 + 注入 Skill 清单 + prompt）、
`main.py`（挂载 skills 路由）、`schemas.py`（Skill 模型）、`routers/chat.py` / `routers/me.py`
（工具清单返回 registry）。

## 3. 验证结果（2026-11-05）

- **单元测试**：`cd backend && python -m pytest tests -q` → **23 passed**（原 12 + 新增 11）。
  - 覆盖：Skill 解析、路径逃逸拒绝、用户隔离、私有覆盖内置、非法名、文件读写/局部改、
    patch 歧义、code_run、load_skill 占位符、L1 清单。
- **前端**：`cd frontend && npm run build` → 通过。
- **API E2E**（TestClient，`test/12345678`）：
  - Skill 列表 / 内置列表 / 创建 / 详情 / 修改 / 删除 / 内置只读(405) 全部通过；
  - `/api/mcp/tools` 返回 6 个内置工具 + MCP 工具。
- **真实 LLM E2E**（`deepseek-v4.1-flash`）：
  - 提示模型「先 load_skill，再 file_write 写 greeting.txt，再 code_run 算 6*7」；
  - 实际工具调用序列：`load_skill` → `file_write` → `code_run`；
  - workspace 内 `greeting.txt` 内容为 `hello-e2e`；模型报告 6*7=42；无 error 事件。
- **数据清理**：E2E 临时会话 / Skill / workspace 文件已删除；库恢复原样
  （41 用户 / 102 会话 / 469 展示消息 / 89 历史）。

## 4. 已知边界 / 下一步

- `code_run` 目前是受控子进程，不是强沙箱；后续可换容器或受限账户，接口不变。
- L2 正文目前随 tool_result 返回；若 Skill 正文很大，后续可改为「tool_result 放摘要 + 正文独立注入」。
- Composer 的 Skill 选择器（`/skill` 命令）尚未做，当前通过「设置 → 我的 Skills」管理。
- 下一阶段：subagent / team / task 工具、计划模式、goal、hooks（均基于同一 `Tool` 抽象）。
