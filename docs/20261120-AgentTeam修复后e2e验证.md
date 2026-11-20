# Agent Team 修复后 e2e 验证（2026-11-20 11:20）

## 一句话结论

**一次跑通。** 两个子代理都 `completed`（各 14 轮），两个任务都 `completed`，
产物真落盘（33 KB + 54 KB），全程 **84 次 HTTP 请求全部 200**，零 507。

对比修复前：同样是两个子代理的任务，起了 **4 个 writer** 才写出来，
最少的 4 轮就死（详见 `docs/20261120-排查经验-从卡住到根因的四路交叉.md` §2.1）。

## 1. 环境

| 项 | 值 |
| --- | --- |
| 栈 | `docker compose -p hlzsbxprobe`，端口 18080 |
| 模式 | `HLZ_SANDBOX_MODE=strict`，`BACKEND=service` |
| 用户 | `hx`（`u_c56b8337dd09`） |
| 会话 | `co_4916630a768c` |
| 任务 | 调研特朗普 2026 政策 → 写 markdown 报告（两段，第二段依赖第一段产物） |

镜像内确认三处修复都在：

```
grep -c "DEFAULT_READ_LINES = 2000"  /app/backend/app/builtin_tools.py  → 1
grep -c "registry.check"             /app/backend/app/builtin_tools.py  → 0   （门禁已删）
grep -c "umask(0o002)"               /app/sandbox_service/executor/bwrap.py → 1
```

## 2. 结果

### 子代理

| agent | status | turns | secs |
| --- | --- | --- | --- |
| `policy_researcher` | **completed** | 14 | 142 |
| `report_writer` | **completed** | 14 | 125 |

### 任务

| 任务 | status | owner |
| --- | --- | --- |
| 调研特朗普 2026 年最新政策要点并写入 | **completed** | `sub:policy_researcher` |
| 基于 research/trump.md 撰写报告 | **completed** | `sub:report_writer` |

### 产物

```
644 1000:1000 33070  .subagents/co_4916630a768c/policy_researcher/research/trump.md
644 1000:1000 54854  .subagents/co_4916630a768c/report_writer/reports/trump_report.md
```

目录逐级 `drwxrwsr-x root 1000`（`2775`，setgid + 组可写）：

```
co_4916630a768c/
co_4916630a768c/policy_researcher/
co_4916630a768c/policy_researcher/research/
co_4916630a768c/report_writer/
co_4916630a768c/report_writer/reports/
```

### 内容真实性

第二个产物开头引用了第一个：

> 本报告严格以调研文件《特朗普 2026 年政策要点调研》
> （`/ws/research/trump.md`，检索时间 2026-11-20）为唯一事实来源……

⇒ **依赖关系真的生效了**（writer 读到 researcher 的产物），不是各写各的。

## 3. 三处修复分别对应哪个判据

| 修复 | 本次怎么验的 | 结果 |
| --- | --- | --- |
| ① 遗留目录 507（第三、四层：向下逐级修权限 + chgrp） | 产物真的落盘 + 父目录逐级 `2775 gid=1000` | ✅ |
| ② code_run umask 022→002（第五层） | 上轮已单独真机验过（`2775`/`0664`）；本次产物都经 `file_write`，未触及 | ✅（另有专项探针） |
| ③ 删除写前必读门禁 + Read 2000 行 | 子代理 `result_text` 里 0 次「只读了片段」；两个 33–54 KB 文件一次读全 | ✅ |

## 4. ★ 本次探针自己的缺陷（产品没问题，探针崩了）

探针日志只有两行就 `Traceback`：

```
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
```

根因：`POST /api/sessions/{sid}/messages` 返回的是 **SSE**
（`routers/chat.py:1011`，`media_type="text/event-stream"`），
我却对**所有**响应都 `json.loads()`。

**最坑的地方：请求其实成功了**——任务照样跑起来、子代理照样完成。
只是探针在读响应体时崩了。

这种「探针崩了但产品没问题」极易被误判成产品故障：
- 日志只有两行就 Traceback ⇒ 看起来像发消息失败
- 但 DB 里子代理正常在跑、产物正常落盘

修法：给 `_req` 加 `raw=True`，SSE 端点不解析响应体。

> 这条与我刚写进 `AGENTS.md` §3.4「判据必须能区分两种状态」是同一类问题：
> **探针的崩溃和产品的故障，在日志里长得一模一样。**

## 5. ★ 本次还抓到一个 grep 假阳性

统计 507 时第一版用：

```bash
docker logs ...-sandbox-1 --since 25m | grep -ciE "507|Permission denied"
# → 1
```

看着像"还有 1 次 507"。实际命中的是**端口号 `50742`**：

```
INFO:  127.0.0.1:50742 - "GET /v1/health HTTP/1.1" 200 OK
```

正确判据是看状态码而不是任意数字：

```bash
docker logs ... | grep -cE 'HTTP/1.1" 507|PermissionError'   # → 0
docker logs ... | grep -oE 'HTTP/1.1" [0-9]{3}' | sort | uniq -c
#   84 HTTP/1.1" 200
```

**这正是 `docs/20261120-排查经验-从卡住到根因的四路交叉.md` §3.4 说的问题**：
一个信号在「修好」和「没修好」两种状态下读数不同吗？
`grep 507` 会被端口号、时间戳、字节数命中，它在两种状态下都可能非零 ⇒ 不是判据。

## 6. 复现

```bash
# 1) 确认镜像内代码是最新的（见 §1 的三条 grep）
# 2) 跑
cd /d/git_codes/inner_chatgpt
HLZ_PROBE_TIMEOUT=600 python -u work/_e2e_team3.py

# 3) 看 DB
docker exec hlzsbxprobe-postgres-1 psql -U hlz -d hlaudz -c \
  "SELECT name,status,turns FROM subagent_runs WHERE session_id='co_4916630a768c';"

# 4) 看产物与权限
docker exec hlzsbxprobe-app-1 sh -c \
  'find /app/backend/data/workspaces/u_c56b8337dd09/.subagents/co_4916630a768c \
   -type f -exec stat -c "%a %U:%G %s %n" {} \;'
```

## 7. 与上一轮的对比

| | 修复前（`co_8abfb27422c8`） | 修复后（`co_4916630a768c`） |
| --- | --- | --- |
| 撰写任务的子代理数 | **4 个** writer 先后起停 | **1 个** writer 一次完成 |
| 最少轮次 | 4 轮就死 | 14 轮正常完成 |
| 任务终态 | completed（但代价极高） | completed |
| 507 次数 | 有（PermissionError 反复） | **0** |
| 「只读了片段」 | 有（writer_c 原话引用） | **0** |

> ⚠️ 样本量只有 1 次。上一轮那种「起 4 个 writer」也可能有 LLM 上游 502 的
> 叠加因素（上轮实测 72 次 502）。要更强结论需连跑 3 次，
> 按 `AGENTS.md` §2 的要求本来也该连跑 3 次才算无 flake。
> 本次因为耗时（~4.5 分钟/次）只跑了 1 次，**结论按 L2 级别看待**。

## 8. 第二次运行（`co_f5276a34b01b`）：被上游内容过滤卡住，但**恰好证明了修复**

第二次是为了验证探针修好（`raw=True`）而跑。探针这次正常输出了轮询进度
（不再 Traceback），但产品侧卡在 `running` 0 轮超过 10 分钟。

### 8.1 卡住的根因是 LLM 上游，不是我们的代码

```
WARNING app.llm_client: LLM 流中断但有部分输出，降级为非流式重试（第 1 次）：
  provider error: litellm.APIConnectionError: OpenAIException -
  Output data may contain inappropriate content.
```

这是**上游的内容安全过滤**（话题是特朗普政策，容易触发）。
app 侧零 502、零 PermissionError —— 不是我们的问题。

### 8.2 ★ 但它恰好给了门禁修复的决定性证据

卡住之前，该 researcher 做了两次文件操作，**都成功了**：

```
11:35:52  文件写入 ... /researcher/trump_draft.md  chars=35000  POST /v1/write  200 OK
11:36:17  文件修改 ... /researcher/trump_draft.md  chars=34955  POST /v1/patch  200 OK
```

**这一对正是修复前必挂的组合**：

| 操作 | 修复前会怎样 | 本次结果 |
| --- | --- | --- |
| `file_write` 覆盖一个 **35 KB** 已存在文件 | 「只读了片段，请 file_read 读完整文件后再写」（`DEFAULT_READ_LINES=200` ⇒ `complete` 恒假） | **200 OK** ✅ |
| 紧接着 `file_patch` 同一个文件 | 「本次会话还没有读过」/「只读了片段」 | **200 OK** ✅ |

而且它是**先 write 建文件、再 patch 改同一个文件**，中间没有先 `file_read` 全文 ——
正是旧门禁会拦的路径。

### 8.3 产物与权限（数字核对，不看名字）

```
文件：1000 1000 644   researcher/trump_draft.md（52469 B）
目录：0    1000 2775  co_f5276a34b01b
目录：0    1000 2775  co_f5276a34b01b/researcher
```

> ⚠️ `ls -l` 在 app 容器里显示 `UNKNOWN:UNKNOWN`，因为**容器里没有 uid 1000 的
> 用户名映射**（sandbox 容器才有 `sandbox` 用户）。这不是权限问题 ——
> 用 `stat -c "%u %g %a"` 看数字才是判据：
> 文件属主 `1000`、属组 `1000`、权限 `644`；目录属主 `0`（root 建的）、
> 属组 `1000`、权限 `2775`。全部符合设计。
>
> **这条本身是个判据教训**：`ls -l` 的 `UNKNOWN` 看起来像"属主异常"，
> 实际只是名字解析失败。要判属主必须看数字。

### 8.4 两次运行合起来的结论

| 判据 | 第一次 `co_4916630a768c` | 第二次 `co_f5276a34b01b` |
| --- | --- | --- |
| 507 / PermissionError | 0 | 0 |
| 覆盖写大文件（>200 行） | ✅ 成功（33 KB / 54 KB） | ✅ 成功（35 KB） |
| 不读直接 patch | 未触及 | ✅ **成功**（这是旧门禁的必挂路径） |
| 目录权限 | 逐级 `2775 gid=1000` | 逐级 `2775 gid=1000` |
| 任务终态 | 两个 `completed` | 卡在上游内容过滤（**非产品缺陷**） |
| 探针是否崩溃 | 崩（SSE 未按 raw 读） | **不崩**（已修） |

**三次修复（507 权限 / umask / 读门禁）在两次真机运行里都拿到了正面证据。**
第二次没跑完是上游 LLM 的内容过滤导致的，与这三处修复无关。

## 9. 第三次运行（`co_f49ca93ebc14`）：卡在首帧前奏之前

为凑够「连跑 3 次」而跑。结果卡住，**但卡点与三处修复无关**。

### 9.1 现象

| 项 | 值 |
| --- | --- |
| 租约 | `status=active` 持续 **655 秒+**（11 分钟） |
| 子代理 | `researcher` `running` **0 轮** |
| 产物 | **0 个文件** |
| assistant 消息 | `status=streaming`，内容 `.......`（7 字符） |
| 首帧前奏日志 | **一条都没有** |
| app 侧 LLM 活动 | **零**（过滤掉探针轮询后什么都没有） |
| 507 / PermissionError | **0** |
| 上游 502 / 内容过滤 | **0** |

### 9.2 排查过程（四路交叉，按 `AGENTS.md` §3）

| 路 | 拿到什么 | 结论 |
| --- | --- | --- |
| ① 现象 | 探针只在状态变化时打印，卡在 `[30s]` 那一行 | 无法独自判因 |
| ② DB | 租约 `active` 655s、子代理 0 轮、消息 `streaming` | **消息已落库 ⇒ 生成器跑过**，卡在生成器内部 |
| ③ 日志 | 零 LLM 调用、零错误；MCP 每 5 分钟重连一次（正常保活） | 不是 LLM/MCP 问题 |
| ④ 文件系统 | 0 个产物；`/proc` 显示 PID 7 状态 `S`（可中断睡眠） | 在等一个永不返回的 await |

额外验证：四个 MCP 端点（`exa` / `tavily` / `sqlbotmcp:8765` / `falcomm:8889`）
**全部可达** —— 排除 MCP 连接超时。

### 9.3 一条重要的推断修正

我一开始想说「零 prelude 埋点 ⇒ 生成器从未执行」。**这是错的。**

`prelude.emit()` 只在正常完成路径（`routers/chat.py:815`）调用；
卡住时报错路径（`:438` / `:450`）只在异常时 emit。
所以「没有 prelude 日志」只证明**没跑完**，不证明没跑。

正确的证据是 DB：`messages` 表里有 `assistant / streaming / .......` 这条记录
⇒ `_event_stream` 的 `add_msg` span（`chat.py:404`）已经执行过 ⇒ **生成器确实跑了**。

> 这条是本轮第 N 次「判据选错」：**「没有某条日志」不能证明「某个动作没发生」**，
> 只能证明「那个日志点没被执行到」。要判「有没有跑到某步」，得找那步的**副作用**
> （这里是 DB 里的消息行），不是找它的日志。

### 9.4 三次运行的完整对照

| | `co_4916630a768c` | `co_f5276a34b01b` | `co_f49ca93ebc14` |
| --- | --- | --- | --- |
| 结果 | ✅ 一次成功 | ⚠️ 成功但极慢 | ❌ 卡住 |
| 子代理轮次 | 14 + 14 | **38 + 26** | 0 |
| 耗时 | 142s + 125s | 671s + 325s（约 11 分钟） | >12 分钟卡住 |
| 产物 | 2 个文件 | 2 个文件 | **0** |
| 507 / 权限错 | 0 | 0 | 0 |
| 根因 | — | 上游内容过滤致重试 | **首帧前奏后卡住（未定位）** |

### 9.5 这次验证的诚实结论

**三处修复（507 权限 / umask / 读门禁）没有被推翻，全部拿到正面证据。**
但它们不是「跑 3 次全绿」——第三次根本没跑到任何工具调用就卡住了。

按 `AGENTS.md` §2 的标准，**这次不算 L3 通过**：

- 修复有效性：**L2**（两次运行正面证据，第三次未能复验）
- 稳定性：**未达标**（3 次里有 1 次卡住、1 次慢到 11 分钟）

### 9.6 待查（本次未定位，不猜结论）

第三次卡住的根因**没有定位**。已知的排除项和线索：

**已排除**：

- 507 / 权限（0 次）
- LLM 上游（0 次 502 / 内容过滤）
- MCP 连接（4 个端点全可达）
- 会话租约冲突（该用户只有 1 个 active generation）
- 沙箱槽（本次没有任何工具调用，没进沙箱）

**线索**：

1. 第三次在第二次结束后 **17 秒**就发起（`03:43:46` → `03:44:03`）
2. 第二次异常慢（38 轮 / 671s），期间触发过上游内容过滤重试
3. app 进程 PID 7 状态 `S`（可中断睡眠），不是死循环
4. 沙箱容器里有僵尸进程：`ps -eo stat,comm | grep -c '^Z'` → **3**，
   但其中 **1 个是我自己造成的**（`grep -c 建槽 /proc/1/fd/1` —— 我为了
   看服务日志去读容器 init 的 stdout fd，结果把管道挂住了 5 分钟，
   `kill -9` 后留下僵尸）。**产品的僵尸只有 2 个**，都是 `[bwrap]`
   （40 分钟 / 23 分钟），**PPID 都是 1（uvicorn）** ⇒ 父进程没调 `wait()`。

   > 这是我本轮的操作失误：**不要读容器 init 进程的 `/proc/<pid>/fd/1`**
   > —— 那是一个管道，读它会一直阻塞。看容器日志用 `docker logs`。

   这两个 bwrap 僵尸是**另一个独立问题**（`killpg` 已修，缺 reap），
   但僵尸不占 CPU，不是这次卡住的原因。

**下一步该查的方向**（需真实复现，不能靠猜）：

- `_event_stream` 在 `add_msg` 之后、第一次 LLM 调用之前的 await 链
  （runtime 构造 / 压缩检查 / 注入装配），看哪一段可能永不返回
- 两次会话紧邻时是否有共享状态未释放（上一次的 MCP 连接 / 沙箱槽 / 缓存）
- 给 prelude 加「卡住超 N 秒就 emit 一条」的看门狗 —— 现在的实现只在正常
  完成或异常时 emit，**卡住这条路径完全没有可观测性**

> 最后一条其实是最有价值的产出：**当前的可观测性对「卡住」是盲的**。
> 这次能定位到「卡在 add_msg 之后」，靠的是 DB 副作用，不是日志。
> 要让下次能直接看到，需要 prelude 每段进出都打一条（或加看门狗）。

### 9.7 ★ 补充观察（探针 900s 超时退出后回查）

后台探针跑满 900s 才退出，触发了我做进一步核对，发现**问题比最初判断更严重**：

#### 9.7.1 我手动「清理」并没有真的解除卡住

我手动执行过：

```sql
UPDATE subagent_runs SET status='stopped' WHERE session_id='co_f49ca93ebc14';
UPDATE session_generations SET status='failed', finished_at=... WHERE ...;
```

回查结果（清理后再等 20 分钟）：

| 对象 | 状态 | 说明 |
| --- | --- | --- |
| `messages.status` | **仍是 `streaming`** | 没有转成 `error` 或 `complete` |
| `assistant.content` | **仍是 `.......`**（7 字符） | 没有任何新增 |
| prelude 日志 | **仍然一条都没有** | 从未 emit |
| `team_tasks` | `in_progress`（`owner=sub:researcher`） | 任务仍被占用 |
| 子代理 `researcher` | 已被我 UPDATE 成 `stopped` | 但**没有 executor 真的停下它** |

**结论：DB 里改状态 ≠ 解除卡住。** 那个 `_event_stream` 生成器还挂在
app 进程的 asyncio 里，它既没完成、也没抛出、也没被取消 —— 所以：

- 错误路径（`chat.py:438` / `:450`）的 `prelude.emit()` 也没跑到
- 消息永远停在 `streaming`，用户刷新页面会看到一条**永不结束的回复**
- 任务永远 `in_progress`，占用 owner

#### 9.7.2 探针本身还有一个缺陷（第二次踩到同类）

第三次探针只打印 `tasks`，而 `GET /team` 的返回里**没有 `runs` 字段**
（`routers/team.py:65-78`，只有 `team` / `tasks` / `events` / `messages`）。
所以探针的 `runs = team.get("runs") or team.get("agents") or []` 恒为空，
**它对子代理状态完全是盲的**。

这解释了为什么日志里只有 task 状态的变化：子代理起没起、跑了几轮，
探针一次都没看到。（第一次/第二次能看到，是我另外查了 DB。）

> 这是同类失误的第二次：**探针拿到的字段不存在时静默降级成空列表**，
> 于是它"看起来在工作"。正确做法是：字段缺失时**报错退出**，不是 `or []`。

#### 9.7.3 修正后的严重性评级

| 项 | 原判断 | 修正后 |
| --- | --- | --- |
| 卡住能否自行恢复 | 未判断 | **否** —— 20 分钟无变化，无异常、无超时、无取消 |
| 手动清理是否有效 | 以为有效 | **无效** —— 只改了 DB 行，进程内生成器仍在 |
| 用户可见后果 | 「卡住」 | **一条永不结束的 streaming 消息 + 任务永久被占用** |
| 探针能否发现 | 以为能 | **不能** —— `/team` 无 `runs`，且缺字段时静默降级 |

这是一条**比 507 更严重的缺陷**：507 至少会报错、会让子代理重试；
而这条是**静默挂死**，没有任何超时或自愈路径。

#### 9.7.4 下一步（优先级由高到低）

1. **给 `_event_stream` 加整体超时 / 看门狗** —— 当前整条链路没有任何
   "卡住超过 N 秒就中止"的机制。这是根因级修复。
2. **prelude 加分段看门狗**：进入每个 span 时打一条，超过阈值未退出也打一条。
   现在的实现只在正常完成/异常时 emit，**卡住路径零可观测性**。
3. **修探针**：`/team` 不返回 runs 时应当**报错**，而不是 `or []` 静默降级；
   子代理状态应从 DB 或专门的接口取。
4. 修 bwrap 僵尸 reap（独立小问题，PPID=1 说明父进程没 `wait()`）。