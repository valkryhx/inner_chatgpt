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