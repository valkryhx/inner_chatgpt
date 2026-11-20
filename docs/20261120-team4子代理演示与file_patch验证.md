# 4 子代理演示（2 并行查数 → 写文档 → 画图）：file_patch 使用与卡顿验证

日期：2026-11-20
会话：run1 `co_f8c4fc556e7e` / run2 `co_0ff7d9692b69`
数据：`agent-activity` 时间轴（654 / 626 条）

## 0. 结论

**两轮都没有卡住，并且模型确实在主动使用 `file_patch`（只发 diff）。**

| 判据 | run1 | run2 |
| --- | --- | --- |
| 总时长 | 482.5s | 532.9s |
| 4 个子代理 | 全部 completed | 全部 completed |
| **>30 秒静默** | **0 段** | **0 段** |
| 最大静默 | 11.1s | 28.2s |
| `file_patch` 调用 | **9 次** | **8 次** |
| `file_write` 调用 | 6 次 | 5 次 |
| assistant 消息 | `complete` 3057 字 | `complete` 873 字 |

**`file_patch` 的调用次数已经超过 `file_write`** —— 说明工具描述里
「只发差异，不要发整个文件」的引导生效了。

### 0.1 产出与速率（run1）

| 指标 | 值 |
| --- | --- |
| 正文 | 14,650 字 |
| 思考 | 72,470 字 |
| 合计 | 87,120 字 |
| 速率 | **180.6 字/秒** |

87,120 ÷ 180.6 ≈ 482 秒，与实测 482.5s 吻合 —— 与之前几轮的结论一致：
**时长由生成字数决定。**

## 1. 任务结构（自然表述，没有点名并行/串行）

发给 agent 的原话：

> 用 team 派 4 个 sub，基于 sqlbotmcp 演示一下：
> 先 2 个并行分头查宽带业务数据（一个查用户规模类指标，一个查渗透/占比类指标），
> 两个都查完以后再写一份文档汇总，最后基于文档出一张图。
>
> 要求：查询用 sqlbotmcp 不要联网；文档要基于真实查到的数字别编；
> 图用 chart_spec 生成；最后告诉我落了哪些文件。

**模型自己把它拆成了 4 个任务**（`team_tasks`）：

```
【查询A】家宽用户规模   → query_scale / worker_scale
【查询B】家宽渗透/占比  → query_penetration / worker_ratio
【汇总】基于查询A/B     → writer_doc / worker_doc     （依赖前两个）
【出图】用 chart_spec   → chart_builder / worker_chart（依赖汇总）
```

## 2. 并行与串行都真的发生了

### 2.1 并行：两个查询同时进行

run1 的 60–175s 窗口内，工具调用**成对交错**：

```
61s, 61s, 62s, 62s, 65s×4, 69s×2, 80s×2, 85s×2, 89s, 93s, 97s, 99s, 103s ...
```

两个子代理同时在查，不是排队。DB 里也印证：`query_scale` 137s 完成、
`query_penetration` 168s 完成 —— 它们的活跃期重叠。

### 2.2 串行：写文档与画图的依赖真的生效

任务板上写着依赖关系（第二个任务的 `blocked_by` 指向第一个），
所以 `writer_doc` 在 183s 才开始（两个查询都完成后），
`chart_builder` 在 367s 才开始（文档完成后）。

这是**真实依赖**，不是调度浪费 —— `wait_agent` 的 145.7s / 105.3s / 57.9s
三段等待期间，子代理侧各有一万多条事件，它在真干活。

## 3. ★ file_patch 的使用形态（核心验证）

run1 的 9 次 `file_patch`：

| 时间 | 路径 |
| --- | --- |
| t=97.1s | `build_scale.py` |
| t=123.2s | `team_demo/data_penetration.md` |
| t=128.8s | `team_demo/data_penetration.md` ← **同一文件第二次** |
| t=240.8s | `team_demo/宽带业务数据汇总_2026-07.md` |
| t=390.9s | `.subagents/…/chart_builder/compose_chart_json.py` |
| t=392.7s | `compose_chart_json.py` |

**关键观察：`data_penetration.md` 被连改两次。**

这正是「只发 diff」的价值场景——如果每次都 `file_write` 整个文件，
第二次修改要重新生成整篇内容；用 `file_patch` 只发改动的几行。

### 3.1 耗时对比（两种工具都很快）

| 工具 | 次数 | 总耗时 | 平均 |
| --- | --- | --- | --- |
| `file_patch` | 9 | 0.23s | **26ms** |
| `file_write` | 6 | 0.16s | **26ms** |

**两者耗时相同** —— 所以选 `file_patch` 的收益不在 I/O，
而在于**模型输出的 token 量**：改 3 行 vs 重写整个 16 KB 文件。

按 run1 的产物看，`宽带业务数据汇总_2026-07.md` 是 16,180 B。
若模型每次都整文件重写，光这一个文件就要多生成几千字
（按 163 字/秒算，每次多 20–30 秒）。

## 4. 没有卡住的证据

| 判据 | run1 | run2 | 说明 |
| --- | --- | --- | --- |
| >30s 静默 | **0 段** | **0 段** | 全程持续有动作 |
| 最大静默 | 11.1s | 28.2s | 都在正常 LLM 生成范围内 |
| 子代理终态 | 4/4 completed | 4/4 completed | 无 stuck |
| 产物落盘 | 4 个文件 | — | 权限 `644 1000:1000` |

对比之前用 exa 的版本（最大静默 301.5s、6 次 300 秒超时），
这一轮换成本地 sqlbotmcp 后**最大静默只有 11–28 秒**。

## 5. 本轮抓出的一个观测缺口（已修）

### 5.1 问题

run1 的时间轴里，子代理的字数行长这样：

```
action=thinking brief=+450 字
```

**看不出是哪个子代理写的。** 4 个代理并行时，「谁在干活、谁卡住」
恰恰是最需要的信息，而这个聚合把 agent 归属丢掉了。

根因：聚合缓冲按 `kind`（text / reasoning）分桶，
没有按 `(kind, agent)` 分桶。

### 5.2 修法

`backend/app/activity_trace.py` 改成 `self._pending: dict[tuple[str, str], int]`，
键是 `(kind, agent)`。落盘时带上 agent：

```
action=reasoning brief=sub:worker_ratio +535 字     ← 修复后
```

run2 验证生效，且能算出每个子代理的产出：

| 子代理 | 产出字数 |
| --- | --- |
| `worker_ratio` | 35,494 |
| `worker_doc` | 15,918 |
| `worker_scale` | 15,154 |
| `worker_chart` | 12,352 |

### 5.3 测试

4 条新测试锁住这个行为（`test_agent_activity_trace.py`）：
- 聚合行必须带 agent 归属
- 不同 agent 的字数不能合并成一条
- 根代理的字数行要能识别
- 子代理的工具调用行要能看出是谁

全部做过「移除实现后测试重新变红」的反向确认。

## 6. ✅ 已修：`sub.tool_start` 的 agent 归属（2026-11-20 当轮补完）

### 6.1 原来的缺陷

接入侧写的是：

```python
trace.record(f"sub.{it}", name=str(inner.get("name") or aid), ...)
```

`name` 位被**工具名**占了（`code_run` / `file_read`），agent id 只能 `or` 兜底 ——
有工具名时就永久丢失。日志变成：

```
action=sub.tool_start brief=code_run code=...
```

多代理并行时无法回答「这个工具调用是谁发的」，也就无法把工具耗时归到
各 agent 头上、无法定位「哪个 agent 卡在工具里」。

### 6.2 修法：`agent` 独立参数 + 拼进 `who=`

`record()` 加 `agent` 参数，与 `name`（工具名）解耦。渲染时拼进 `who=`：

```
修复前： who=root t=155020ms action=sub.tool_start brief=code_run ...
修复后： who=root/sub:worker_c t=155020ms action=sub.tool_start brief=task_get ...
```

**为什么拼进 `who=` 而不是新增 `agent=` 字段**：新增字段会插在 `who=` 与 `t=`
之间，而现有分析脚本用 `who=(\S+) t=` 紧邻匹配 —— 中间多一个字段会让那些
正则**静默失配**（不报错，只是匹配不到任何行，看起来像「那轮没有日志」）。
拼进 who 则 `\S+` 自然吞掉 `root/worker_ratio`，旧脚本照常工作。

**为什么 `brief` 首 token 必须仍是工具名**：现有脚本用 `brief.split()[0]`
取工具名做 start/result 配对。agent 放进 brief 开头会让所有这类脚本失效。
所以 agent 只走 `who=`，`brief` 结构不动。

### 6.3 修复带来的新能力：按 agent 归集工具耗时

run3（`co_78daf507d365`，4 个子代理，288.7s，0 段 >30s 静默）：

| agent | 工具调用 | 耗时 | top 工具 |
| --- | --- | --- | --- |
| `root` | 36 次 | 175.6s | `metric_search`×5, `task_create`×4, `spawn_teammate`×4 |
| **`root/sub:worker_b`** | **26 次** | **7.0s** | **`run_readonly_sql`×10**, `code_run`×7 |
| `root/sub:worker_a` | 14 次 | 0.6s | `code_run`×8 |
| `root/sub:worker_c` | 11 次 | 0.3s | `code_run`×7 |
| `root/sub:worker_d` | 7 次 | 0.2s | `code_run`×3 |

**这张表在修复前做不出来。** 它直接回答了「哪个子代理在工具上花时间」：
`worker_b` 用了 10 次 `run_readonly_sql`，耗时 7 秒，是其他三个子代理的十倍以上。
若某个 agent 卡在工具里，现在一眼能看出来。

### 6.4 测试与守卫

| 测试 | 锁住什么 |
| --- | --- |
| `test_sub_tool_start_carries_agent` | 工具行能看出 agent（断言**渲染后的日志行**） |
| `test_tool_name_still_first_token_of_brief` | brief 首 token 仍是工具名（旧脚本兼容） |
| `test_agent_rendered_under_who_not_new_field` | 拼进 who=，不新增字段（旧正则兼容） |
| `test_chat_py_passes_agent_explicitly` | **静态守卫**：接入侧不能退回旧写法 |

静态守卫用 AST 按调用点检查，不用纯文本匹配 —— 注释里会合法提到旧写法，
纯文本会让守卫假红。反向确认验证过：把接入侧改回 `name=... or aid`，守卫立刻变红。

> 为什么这个缺陷必须靠静态守卫：它**不报错、不影响功能**，
> 只表现为日志少一块。真机多代理 + 确有工具调用时才暴露，单测很难锁住。