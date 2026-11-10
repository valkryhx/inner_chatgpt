---
name: hlaudz-e2e
description: 用真实 LLM 对 Hlaudz 跑端到端验证。改动 subagent、Agent Team、内置工具、提示词、SSE 路由、停止/回收逻辑后必须使用；也用于「UX 修好了」的验收。产出 runId + 事件落盘 + 可复盘的断言报告，而不是读 stdout 猜。
---

# Hlaudz 真实 e2e 验证

## 何时必须用

以下任一情况，**不跑这个 skill 就不许说"修好了 / 已验证"**：

1. 改了 `backend/app/subagent/`、`team_host.py`、`team_store.py`、`team_tools.py`；
2. 改了 `backend/app/builtin_tools.py` 或新增工具；
3. 改了 `backend/app/agent.py` 的 `SYSTEM_PROMPT`、`subagent/prompts.py`；
4. 改了 `routers/chat.py` 的 SSE 流、停止、回收、事件合并；
5. 修复的是「链路级」缺陷（通知丢失、僵尸状态、关闭竞态、任务悬挂）；
6. 用户说「测一下 / 验证一下 / 看看效果」；
7. 要声称某个 UX 行为「用户能看到」。

单测（`pytest backend/tests`）只能证明**分支逻辑正确**，不能证明链路能跑通。
GA 项目在单测全绿时用真实 e2e 抓出 12+ 个链路缺陷，全部是单测原理上测不到的。

## 铁律（违反任何一条，本轮结论作废）

1. **不读 stdout 做断言**。SSE 事件逐行落盘到 `runs/<runId>/events.jsonl`，事后按 `type` 过滤。
   累积字符流里会混入 ANSI、重排、半截 JSON，是最常见的假阳性来源。
2. **每次运行生成唯一 `runId`**（时间戳 + 随机 4 位）。所有断言都带 runId，
   避免读到上一次残留。
3. **哨兵 token**：给子代理/teammate 的任务里塞一个本次唯一的随机串（如 `SENTINEL=7f3a91`），
   要求它在最终结果里复述。没复述 = 消息没送达，不是「模型没听话」。
4. **负对照**：同一 harness 跑一个不该触发该行为的任务，确认它没发生。
   只断言「用了 X」不够，要证明「不该用的时候没用」。
5. **保留现场**：失败不清理 `runs/<runId>/`，写明路径。
6. **PASS 不算通过**：必须跑 `python -m tests.e2e.audit <runId>`，对比 SSE 与数据库。
   断言只写在「工具被调用过」上，等于没测（见 §6.7 假绿三问）。

## 1. 前置检查（跑之前逐条确认）

```bash
cd /d/git_codes/inner_chatgpt
# ① 后端是否在跑
curl -s http://127.0.0.1:8000/api/health
# ② 是否有可用的真实 LLM 配置（model 非空才说明配好了）
curl -s http://127.0.0.1:8000/api/health | python -c "import sys,json;print(json.load(sys.stdin).get('model'))"
# ③ 数据库位置（隔离测试库时用）
ls -1 backend/data/*.db 2>/dev/null
```

后端没起就先起（A2A 与本 skill 无关，普通启动即可）：

```powershell
cd D:\git_codes\inner_chatgpt\backend
python run.py
```

**不要用假 LLM 跑这个 skill**。假 LLM 属于 L2 集成测试，它证明不了链路。

## 2. 被测 API 契约（照这个发请求）

| 用途 | 方法/路径 | 说明 |
| --- | --- | --- |
| 注册 | `POST /api/auth/register` | `{username, password}` → token |
| 登录 | `POST /api/auth/login` | `{username, password}` → token |
| 建会话 | `POST /api/sessions` | `{title?}` → `{id}` |
| **发消息（SSE）** | `POST /api/sessions/{sid}/messages` | `{text, generation_id?}`，响应 `text/event-stream` |
| 停止 | `POST /api/sessions/{sid}/stop` | `{message_id?}` |
| 列消息 | `GET /api/sessions/{sid}/messages` | 事后核对持久化 |
| 会话上下文 | `GET /api/sessions/{sid}/context` | 核对 token 用量 |

认证头：`Authorization: Bearer <token>`。

SSE 的每一行形如 `data: {...}`，以空行分隔事件；`[DONE]` 或流关闭表示回合结束。

## 3. harness 契约（`backend/tests/e2e/harness.py`）

如果没有这个文件，先按以下契约实现它——**这是本 skill 的一部分，不是可选优化**：

```python
async def run_case(case: dict, *, user: str = "e2e") -> RunResult:
    """跑一个场景，返回 runId + 事件列表 + 断言结果。

    case: {
      "name": str,
      "prompt": str,              # 发给 root agent 的文本
      "expect_tools": [str],      # 必须出现的工具名
      "forbid_tools": [str],      # 一次都不许出现的工具名（负对照的关键）
      "sentinel": str | None,     # 必须出现在最终助手消息里的串
      "timeout_s": int,
      "asserts": [callable],      # 额外断言，入参是 events
    }
    """
```

落盘：

```
backend/tests/e2e/runs/<runId>/events.jsonl   # 原始 SSE 事件
backend/tests/e2e/runs/<runId>/summary.json   # 工具序列/耗时/终态/断言结果
backend/tests/e2e/runs/<runId>/messages.json  # 持久化后的消息列表
```

## 4. 场景矩阵（按改动范围挑，不是每次都全跑）

| ID | 场景 | 提示词要点 | 必查事实 |
| --- | --- | --- | --- |
| S1 | subagent 基础 | 「派一个子代理算 17×23，把结果和 SENTINEL 一起告诉我」 | `spawn_agent → 完成 → read_agent_result`；sentinel 复述 |
| S2 | subagent 并行 | 「同时派 3 个，分别算 A/B/C」 | 1 回合内 3 个 spawn；3 个终态；**无重复 spawn** |
| S3 | wait 超时 | 「派一个要跑 120 秒的任务，先别等」 | 超时后子代理仍 running；后续能拿到结果 |
| S4 | steer | 「派统计任务，中途追加一条要求」 | `followup_task` 后新指令被复述 |
| S5 | team 基础 | 「建 3 个任务，分给 2 个 worker」 | 任务板全 completed；无 zombie running |
| S6 | team 规模 | 「10 个简单 + 3 个中等任务」 | 全部完成；面板不静默截断 |
| S7 | 取消 | 「派 2 个，然后取消其中一个」 | 取消的终态 stopped；另一个正常；槽位释放 |
| S8 | 工具选择 | 「在 backend 目录找所有 TODO 并统计」 | 出现 `file_grep`，**不出现** `code_run` |
| S9 | 并发槽位 | 「同一轮一次性派 6 个（每个先 sleep 8 秒）」 | 前 4 个成功；第 5/6 个返回「已达上限 4」；模型 close 后补派；最终 6 个 completed |
| S10 | 长任务 | 「派一个 sleep 660 秒的子代理」 | 跨多轮 wait 后拿到结果；无重复 spawn；事件缓冲不丢 |
| S12 | 跨回合通知 | 两轮对话：第一轮派活后结束，第二轮问结果 | 第二轮不重新 spawn；`read_agent_result` 能取到上一代结果（source=db）；history 含「[任务通知]」 |
| S11（计划） | 大输出 | 「跑 pytest 并列出失败详情」 | 出现落盘路径且模型能读回 |
| S0 | 负对照 | 「今天几号」 | 无 spawn / 无搜索工具 |

**最少跑 S1 + S5 + S0**；改动涉及哪一块就加对应场景。

## 5. 判据三级（措辞不许混）

| 级别 | 证据 | 允许的说法 |
| --- | --- | --- |
| L1 构造得出 | 代码路径存在 | 「机制已实现」 |
| L2 运行时发了 | 事件流里出现 | 「机制已触发」 |
| L3 用户看到了 | 前端渲染 / 截图 / 消息持久化 | 「UX 已修复」 |

只有 L3 能说「用户能看到」。UI 相关改动必须另跑 Playwright 截图，
或至少确认 `GET /messages` 里的持久化内容正确。

## 6. 假阳性清单（先排除这些，再报缺陷）

| 现象 | 更可能的原因 |
| --- | --- |
| 「工具调用成功但结果为空」 | 读的是累积 stdout，尚未收到完整事件 |
| 「子代理全部 failed」 | 后端在子代跑完前被停（内存回收），不是产品缺陷 |
| 「模型没按提示词做」 | 单次采样波动；同一 prompt 连跑 3 次再下结论 |
| 「找不到 session」 | runId 串了，读到了上一次的 DB |
| 「timeout」 | 模型侧慢（上游排队），先看 `summary.json` 的单次 LLM 耗时 |
| 「消息重复」 | 前端重连导致的事件重放，查 `generation_id` 是否一致 |

## 6.5 审计：SSE 与数据库必须一致

跑完场景后必须审计一次，不能只看 `[PASS]`：

```bash
cd backend
python -m tests.e2e.audit <runId>
```

它会给出：

| 字段 | 含义 |
| --- | --- |
| `tools` / `tool_counts` | 工具调用序列（与预期对比） |
| `subagent_started` / `subagent_ended` / `subagent_status` | 子代理终态分布 |
| `zombie_running` | 有 start 没有 end 的名字 → **必须为空** |
| `task_states` | 从 **SSE 事件** 解析出的任务状态 |
| `db_task_states` | 从 **数据库** 读到的任务状态 |
| `sse_missing_events` | DB 有、SSE 没收到的事件数与 seq 列表 |

**判据**：

1. `zombie_running` 必须为空；
2. `task_states` 与 `db_task_states` 必须一致——不一致说明事件链路断了，
   面板会显示错误的进度（2026-11-11 就因为这个抓到 `events_since` 不 publish 的缺陷）；
3. `sse_missing_events.missing_count` 应为 0；非 0 时把 seq 列表写进报告。

## 6.6 方法：找「同一事实的两份独立记录」，然后对比

这是本 skill 里最值钱的一条经验，来自 2026-11-11 抓到的一个真实缺陷。

**核心思想**：一个事实只要被记录两遍以上，就一定会有偏差。差异本身就是 bug 的定位器。
不要去"猜哪里可能有问题"，去**找两份本该相同的记录**，让机器告诉你它们不一致。

### 6.6.1 Hlaudz 里现成的对照对

| 事实 | 记录 A | 记录 B | 不一致意味着 |
| --- | --- | --- | --- |
| 任务状态 | SSE `task_updated` 事件 | `team_tasks.status` | 面板显示错误，数据是对的 |
| 子代理结果 | `subagent_end.result` | `subagent_runs.result_text` / sidechain | 结果丢失或写入错位 |
| 对话内容 | SSE `text_delta` 拼接 | `messages` 表持久化内容 | 前端显示与重载后不一致 |
| 工具产物 | 工具返回的路径 | 文件是否真的存在、内容是否匹配 | 假成功 |
| 事件完整性 | `team_events` / `subagent_messages` 行数 | SSE 收到的事件 seq 集合 | **事件被丢弃**（最难发现） |
| 子代理生命周期 | `subagent_start` 的名字集合 | `subagent_end` 的名字集合 | 僵尸 running |

**最优先做事件完整性**：因为事件是"旁路"的，丢了不会报错，只会让 UI 靜默地显示旧状态。
2026-11-11 的缺陷就是这一类——库里有 43 条事件，SSE 只收到 16 条，没有任何错误日志。

### 6.6.2 怎么执行

1. 跑场景，**不要在跑的时候做判断**；
2. 跑完 `python -m tests.e2e.audit <runId>`；
3. 只看三件事：`zombie_running` 是否为空、`task_states` 与 `db_task_states` 是否相等、
   `missing_count` 是否为 0；
4. 任何一个不满足，**先当作缺陷**，再去找解释（可能是预期的，比如 `team_end`）。

不要因为"页面看起来正常"就跳过——页面只显示最后一次渲染的结果，
恰恰是最容易掩盖"事件丢了"的地方。

## 6.7 假绿三问（场景 PASS 之后必做）

`[PASS]` 只说明你写的断言通过了，不说明功能对。按顺序问：

### 问 1：断言是「发生」还是「结果对」？

- ❌ `expect_tools: ["task_create"]` —— 只证明模型调了这个工具；
- ✅ `expect_all_tasks_completed: True` —— 证明 13 个任务真的都完成了。

2026-11-11 的 S6 在**缺陷存在时也是 PASS 的**，因为当时只断言了工具出现过。
这就是"零缺陷"的假象来源：**断言写在动作上，而不是结果上**。

### 问 2：有没有负向断言？

`forbid_tools` / `tool_counts`（精确次数）必须有。只断言"用了 X"，
模型用错工具（比如用 `code_run` 代替 `file_grep`）也照样 PASS。

### 问 3：关键终态查了两处吗？

工具的返回值（`task_list` 的输出）是"它说完成了"；
数据库里 `team_tasks.status` 才是"真的完成了"；
SSE 里的事件才是"用户真的看到了"。
**三者任意一处没查，都不算验证过。**

---

## 6.8 案例复盘：teammate 事件进不了 SSE

### 现象

S6 规模压测跑完 PASS。审计发现：

```
DB:  13/13 completed，team_events 43 条
SSE: 只收到 16 条，缺 27 条（task_updated 缺 20）
```

页面上的 team 面板会一直显示 11 个任务 `pending`、2 个 `in_progress`，一个都没完成。

### 根因

```python
async def events_since(self, since_seq=0, limit=200):
    rows = await team_store.events_since(self.scope, since_seq, limit)
    for row in rows:
        self._observed_seq = max(self._observed_seq, int(row["event_seq"]))
    return rows          # ← 游标推了，事件没广播
```

teammate 在自己 host 实例里 `_emit()` 写库（正常）→ lead 调 team 工具时拉到这些行
→ 游标推到最新 → **但没 publish，下一轮从新游标开始，永远不再出现**。

### 为什么单测抓不到

- 事件确实写进了库 → 任何"检查 DB 有记录"的测试都通过；
- 游标确实推进了 → 任何"检查游标单调"的测试都通过；
- 缺的是「库里有、流里没有」这个**跨层事实**，它只在两层同时被比较时才暴露。

### 教训

1. **"拉取即丢弃"比"不拉取"更糟**：不拉取还能在下次全量刷新补上；
2. 凡是「旁路同步」的代码（事件镜像、缓存刷新、通知中继），
   都要单独测一条「源有新记录 → 目标真的收到」；
3. 这类代码最容易被写成"只更新游标"，因为它"看起来很忙"。

## 7. 报告模板（贴进回复或 docs）

```markdown
### e2e 结果（runId=<id>）
- 场景：S2 并行 3 个子代理
- 耗时：38.4s（其中 LLM 31.2s）
- 工具序列：spawn_agent ×3 → wait_agent → read_agent_result ×3
- 断言：PASS ×4 / FAIL ×1
- 失败项：第 2 个子代理终态是 `running`，12s 后仍为 `running`（事件末条 seq=87）
- 现场：backend/tests/e2e/runs/<id>/
- 结论级别：L2（运行时发了，未验证前端渲染）
```

失败项必须给：**事件 seq + 最后的 status + 现场路径**。没有这三样，缺陷无法复现。

## 8. 已知坑（GA / Hlaudz 实际踩过）

1. **先 `stripAnsi` 再按 ESC 切帧** → 帧边界丢失。直接按行读 `data:`。
2. **用正则批量改同名键** → 改到了别处的同名字段，测试假绿。用结构化 JSON 操作。
3. **子代理事件与主事件混在一个流里** → 断言时先按 `agent_id` / `type` 分组，别按顺序假设。
4. **后端被内存回收杀掉** → 长跑场景开始前确认内存，失败时先看进程是否还活着。
5. **模型的"完成"是自我声明** → 必须校验产物：文件存在、内容含 sentinel、消息持久化存在。
6. **重名子代理** → 第 2 个同名会被加后缀，断言要用 `list_agents` 返回的真实名字。
7. **实验类并行跑污染数据** → 第二份进程的端口若被第一份占着，它会连上第一份的实例，两种配置的数据混进一张表，读出来像「全都失败」。起服务前先探端口，占了就报错退出。
8. **SSE 显式分支链会静默丢事件** → `chat.py` 是 `elif et == ...` 白名单，新增事件类型（如 `subagent_status`）不写进分支就没有任何日志地消失。验证新事件时，断言必须落在 SSE 帧上（`event: xxx` in joined），不能只测 `publish()`。

## 8.5 稳定性复跑（不是每轮都跑，改完协作链路后跑一次）

```bash
cd backend
python -m tests.e2e.flake --case S1,S2,S3,S4,S5,S6,S7,S8,S11 --rounds 3
```

它会自己把 **上游 502 / ConnectError** 和**真 flake**分开统计：
502 记进「基础设施失败」列，不参与「稳定通过 / FLAKE / 稳定失败」的判定。
直接看 PASS/FAIL 计数会把网关抖动读成产品缺陷。

## 9. 与其他测试层的关系

| 层 | 命令 | 证明什么 |
| --- | --- | --- |
| L1 单测 | `pytest backend/tests -q` | 分支逻辑、边界、CAS、权限 |
| L2 集成 | `pytest backend/tests -q -k integration` | 编排顺序、事件形状（假 LLM） |
| **L3 真实 e2e（本 skill）** | `python -m backend.tests.e2e.run --case S1` | **链路能跑通、模型真的这么用** |

三层全绿才能说「完成」。L1 绿 + L3 红 = 有链路缺陷；L3 绿 + L1 红 = 有边界缺陷。
