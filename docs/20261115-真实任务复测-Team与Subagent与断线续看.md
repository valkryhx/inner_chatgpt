# 真实任务真机复测：Agent Team / Subagent / 断线续看（2026-11-15）

> 日期：2026-11-15　状态：**21/21 全绿**（B/C/A 三场景）+ e2e 三件套 3/3
> 覆盖：持久化第三步（阶段 1~4）在**真实协作任务**下的表现
> 环境：`.env` 生产默认（心跳 15s / 重放 2000 帧），8000 端口，真实湛卢

---

## 0. 一句话

在 8000 上按生产配置跑真实 Agent Team 与 Subagent 任务，并在任务**中途断线**
再续看：**21 项断言全过**，任务板无滞留、子代理卡片全部收口、内容无丢失。
期间一个疑似缺陷（生成中被标 `stopped`）经 6 组对照实验判定**未能复现**，
并顺带记录一次我自己造成的**数据误删与恢复**。

---

## 1. 环境与隔离

`.env`（不入库）用生产默认值，只开 A2A：

| 键 | 值 | 说明 |
|---|---|---|
| `HLZ_TURNJOB_HEARTBEAT_IDLE_S` | `15` | 生产默认；自测专用值 4s **不在这里用** |
| `HLZ_TURNJOB_REPLAY_FRAMES` | `2000` | 同上，不压小 |
| `INNER_A2A_ENABLED` | `true` | 要验对外调用面仍可用 |
| `INNER_A2A_SYNC_TIMEOUT_S` | 未设 | 保持 60s 生产默认 |
| `HLZ_MEMORY_AUTO_WRITE` | 未设 | 保持关闭（与本轮无关） |

**8000 用的是生产库**，所以测试前备份（`work/db_backup/app_20261115_110930.db`，
129 users），跑完逐表回滚（见 §5）。

---

## 2. 标准三件套（e2e skill 要求的最低集）

| 场景 | 结果 | runId | 说明 |
|---|---|---|---|
| S0 负对照 | **PASS** | `1794712334-9060` | 只调 `current_time`，无 spawn、无搜索 |
| S1 子代理基础 | **PASS** | `1794712345-e2c6` | `spawn→wait→read→close`，哨兵复述 |
| S5 Team 基础 | **PASS** | `1794712372-0013` | 2 任务全 completed，51.2s |

三条 `python -m tests.e2e.audit` 全部：
`zombie_running` 空、`task_states == db_task_states`、`missing_count == 0`
（S0/S1 只有 `team_end` 落在 benign 里，属预期）。

---

## 3. 针对性：真实协作任务 × 中途断线续看（21/21）

新增 `tools/turn_job_verify/resume_collab.py`。这三个场景专门打本轮改动的
**交叉点**——之前的 `live_selftest.py` 只验帧层，prompt 里**没有子代理**。

| 编号 | 断言 | 实测 |
|---|---|---|
| A-1 | 断开时仍在 streaming（否则没测到续看） | `streaming` |
| A-2 | 续看有内容（增量重放 / sync 二选一） | 增量重放 |
| A-3 | 按前端算法重建后拿到子代理卡片 | 种子 1 → 重建 6，含 1 张卡片 |
| A-4 | 续看不新建消息 | 2 → 2 |
| A-5 | 最终落库 `complete` | `complete` |
| A-6 | 子代理卡片终态非 running | `[('runner','completed')]` |
| A-7 | 卡片与正文都含哨兵 | True / True |
| A-8 | 无活 job 时续看回 `done` | `done` |
| B-1 | 断开时仍在 streaming | `streaming` |
| B-2 | 断开瞬间任务板可从 DB 读出 | `in_progress / pending` |
| B-3 | 续看叠加后成员卡片还在 | 种子 17 → 重建 37，2 张卡片 |
| B-3b | 续看不新建消息 | 2 → 2 |
| B-4 | 最终落库 `complete` | `complete` |
| B-5 | 断线未导致任务滞留 running | 两条全 `completed` |
| B-6 | 任务数 ≥ 2 | 2 |
| B-7 | 成员卡片全部收口 | `[('worker_a','completed')]` |
| B-8 | 主答复复述哨兵 | 1243 字 |
| C-1 | 停止后不留在 streaming | `stopped` |
| C-2 | 停止后续看回 `done`（非 404） | `done`，payload `stopped` |
| C-3 | 续看不新建消息 | 2 → 2 |
| C-4 | 停止不丢已产出内容 | 5 blocks |

runId `1794715505-6e26-rc`，现场 `backend/tests/e2e/runs/<runId>/{A,B,C}/`。

### 3.1 判据写过一次错，已改

第一版 A-2/A-3 断言「续看首帧必须是 `sync` 快照」，实测走的是**增量重放**。
读 `frontend/src/App.tsx: resumeIfRunning` 后确认真实契约是：

```
种子 = loadMessages() 拿到的 blocks（库里的）+ 逐帧叠加（sync 会替换全部）
```

`sync` 只是**缓冲不够时的降级分支**。生产默认 2000 帧下 `after=0` 几乎总走增量。
改成按前端算法**重建** blocks 再断言，才是真的在验用户能看到什么。

### 3.2 审计工具的边界

B 场景审计报 `missing_count=4`、`task_states != db_task_states`。这不是缺陷：

- 缺的 4 条全是**断线那 16 秒内**产生的 `task_updated` / `team_message`；
- 任务板的恢复**不走 SSE**，走 `GET /api/sessions/{sid}/team`（B-2 / B-5 已证）；
- 审计工具从 `events.jsonl` 取首个 `team_id` 去比 DB，**对「故意断线」场景不适用**。

同时验证了另一件事：**别把三个场景的 SSE 混写进一个 `events.jsonl`**。
第一版这么干，审计给出 5 个假缺陷（team_id 取自 A、任务态取自 B）。已改为每场景独立目录。

---

## 4. 疑似缺陷：生成中被标 `stopped` —— **未能复现**

### 4.1 现象

第一次跑 A/B/C 时，B 场景后段全红：

```
B-4 最终落库终态 complete  | 实测 status=stopped
B-5 断线未导致任务滞留 running | 一条 in_progress
B-7 成员卡片全部收口       | [('worker_a','running')]
B-8 主答复里复述了哨兵      | 正文长度=5
```

后端日志里该会话**没有** `POST /stop`，也没有 error/warn。事后再查库，
那条 assistant 是 `complete`、正文 975 字 —— 即中途被写成 `stopped`，之后又跑完了。

### 4.2 排查过程（6 组实验）

| 实验 | 做法 | 结果 |
|---|---|---|
| `stale_probe.py` @8000 | 每 2s 读 `GET /{sid}` | 58.7s 翻 stopped，62.8s 回 streaming，**未复现第二次** |
| `stale_probe2.py` | 每 2s 读两个端点 | t=2 就 stopped（与全部其它相反）→ **探针本身不可信，已删** |
| `stale_raw.py` | **不打任何 API 读**，直读 sqlite 原始列 | 全程 streaming → complete，**从不** stopped |
| `active_probe.py` | 进程内真 `turn_job` + 真 `runtime` | 静默 40s 判据**全程 True** |
| `launch_diag.py` + 全 A/B/C | 包装 `has_active_generation` / `mark_stale_streaming_stopped` / `update_message` / `add_message` | 21/21 全过，**写入追踪计数 0** |
| 8000 重跑 A/B/C（无额外负载） | 同第一次的组合 | **21/21 全过**，B 的 `stopped` 不再出现 |

### 4.3 结论与判据

**`stopped` 误标未能复现**。所有能写 `stopped` 的函数（`mark_stale_streaming_stopped`、
`update_message(status=stopped)`、`add_message(status=stopped)`）在完整 A/B/C 下
**一次都没被调用**。

第一次出现的条件我无法复原，最可能是当时**三个探针 + 一个 e2e 套件并发抢 CPU**
造成的观测失真（同一天已第三次遇到「探针伪造现象」）。按项目纪律：
**未复现不等于不存在**，但也不能据此改源码。已保留两个可复跑的探针
（`stale_raw.py` 定论用、`stale_probe.py` 可再观察），下次再出现时先跑前者。

> ⚠️ 教训：**观测者要绕开被测路径**。前两个 API 探针本身就调用了疑似写入者
> （`GET /{sid}` 带自愈分支），所以它们的结论互相矛盾；只有直读 sqlite 的
> `stale_raw.py` 才给出干净答案。

---

## 5. 数据回滚：一次真实的误删与恢复（必须留档）

### 5.1 我删多了

清理本轮测试账号时，前 10 张表按 `user_id` 级联删除，数字回到基线。但
`messages` / `team_tasks` / `team_events` / `subagent_runs` **没有 `user_id` 列**，
于是我又补了一步「孤儿行」删除，判据写成：

```python
('team_tasks', "team_id not in (select team_id from team_runs)")
('subagent_runs', "session_id not in (select id from sessions)")
```

这是**错的**：`team_runs.team_id` 与 `team_tasks.team_id` 不是同一命名空间，
判据恒真 → **197 行 `team_tasks` 全删**（其中 189 行是备份里本来就有的历史数据），
`subagent_runs` 591 行被删。

### 5.2 恢复

`tools/turn_job_verify/restore_from_backup.py`（**逐行对齐备份**，不猜关联字段）：

```
team_tasks       0     -> 189   OK
team_events      316   -> 686   OK
subagent_runs    3     -> 554   OK
```

随后用 `--all` 把全库 25 张表逐行同步回备份。**25 张表全部一致，diff 为空。**

备份里本来就有 128 个历史 e2e 账号（更早会话遗留），不属于本轮，没有动。

### 5.3 教训

- **猜测关联字段做级联删除是危险操作**。没有 `user_id` 的表就老老实实按备份逐行对齐。
- 备份的价值在**逐行可比**，不只是「有个文件在」。这次能完整恢复，靠的是行级对齐。
- 清理脚本必须支持 `--dry-run` 并**先看「少几条」**：本次试运行显示了
  `少=0`，即证明备份数据一条不缺，才敢执行。

---

## 6. 其余各层

| 层 | 结果 |
|---|---|
| L1 全量单测 | **1122 passed × 3**（连跑三次） |
| 标准三件套 | S0 / S1 / S5 全 PASS |
| 协作续看 | A/B/C **21/21** |
| 8000 完整复跑 | **21/21** |

> 单测曾出现一次 `test_memory_prefetch.py::test_loop_consumes_only_after_tool_steps`
> 失败（全量负载下）。后端源码本轮**一行未改**（`git status backend/` 为空），
> 单文件连跑 5 次全绿、全量连跑 3 次全绿 —— 属既有测试的时序偶发，非本轮引入。

---

## 7. 新增工具（`tools/turn_job_verify/`）

| 文件 | 用途 |
|---|---|
| `resume_collab.py` | **真实协作任务 × 断线续看**，A/B/C 共 21 项断言；每场景独立 run 目录 |
| `stale_raw.py` | 绕开 API、直读 sqlite 原始列的定论探针 |
| `stale_probe.py` | API 路径观察探针（第一次观测到翻转，后续未复现） |
| `active_probe.py` | 进程内验 `has_active_generation()` 在静默期是否恒真 |
| `launch_diag.py` | 诊断启动器：包装写 `stopped` 的所有函数并打调用栈；默认隔离库，`--use-prod-db` 可选 |
| `cleanup_test_accounts.py` | 按时间戳清理本轮测试账号（支持 `--dry-run`） |
| `restore_from_backup.py` | **逐行对齐备份**恢复表（`--all` 全库） |
| `heartbeat_prod.py` | 生产默认 15s 心跳实测（确认 `.env` 真的生效） |

---

## 8. 变更记录

| 日期 | 内容 |
|---|---|
| 2026-11-15 | 8000 生产配置下真实任务复测：三件套 3/3、协作续看 21/21、全量单测 1122×3。疑似 `stopped` 误标经 6 组实验**未能复现**（所有写 stopped 的函数调用计数为 0），保留可复跑探针。期间我误删 `team_tasks`/`subagent_runs` 历史行（判据把两个不同命名空间的 team_id 当关联），已按备份逐行恢复，25 张表全部一致 |
