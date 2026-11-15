# persistence_verify — 持久化 / 断线续看 / 崩溃恢复 探针

> 从 `work/` 迁入（2026-11-15）。这些探针验的是「进程或连接断了之后，状态对不对」，
> 单测**原理上**测不到 —— 必须真起后端、真断开、真 kill。

## 前置

真实 LLM（湛卢），key 从 `llm.yaml` / 用户配置读，别把 key 写进脚本。

要验 R-2（缓冲溢出降级 sync）时，后端**必须**压小重放缓冲，否则这条路径
在生产默认配置下永远走不到：

```bash
cd tools/persistence_verify
python launch_verify.py     # 监听 8000，HLZ_TURNJOB_REPLAY_FRAMES=20
```

## 脚本清单

### 断线

| 文件 | 验什么 |
|---|---|
| `probe_disconnect.py` | 断开后能不能立刻重发、被掐断的消息最终是什么状态 |

验三件事：

- Q1 断开后立刻重发 → 200 还是 409？
- Q2 若 409，等多久能恢复？
- Q3 那条被掐断的 assistant 消息最终是什么状态？

> **判据演进（为什么不再看文本）**：早期版本用「响应文本里有没有 `generation`
> 这个词」判断是否被租约挡住 —— 那个判据是坏的，正常回复里也可能出现该词，
> 而 409 的 body 里更是必然出现，等于恒真。现在只看**状态码**。
>
> **Q3 必须等到生成真正结束**：早期版本断开后只 `sleep(2)` 就查库，读到
> `status=streaming` 并打印成「被掐断的那条」，读起来像「消息永久卡住」。
> 实际 2 秒时 `len=2146`，最终 `status=complete / len=7535`。现在轮询到非
> streaming 再下结论。

### 续看（阶段 1 / 阶段 2）

| 文件 | 验什么 |
|---|---|
| `probe_job_resume.py` | 断开后生成是否继续到自然结束（阶段 1：解耦） |
| `probe_job_resume2.py` | 新连接能否接上正在跑的那一轮（阶段 2：续看，R-1~R-4） |

`probe_job_resume2.py` 的四个场景（原 R-5「不产生重复消息」已并入 R-1 断言，
它本来就是 R-1 的前后行数对比）：

- R-1 新线续看：新开 → 重连 `GET /stream?after=k` → 收到尾部事件 + 消息行数不变
- R-2 游标太老降级 sync：`after=0` **且 job 仍在运行**时收到 `event: sync` + 完整 blocks
- R-3 无活跑 job：已跑完的会话调 `/stream` 返回 `done`（不是 404）
- R-4 双连接同时看：两个连接收到同样的后续内容

#### R-2 的两个前提（少一个就必然 SKIP）

1. **重放缓冲被压小** —— `HLZ_TURNJOB_REPLAY_FRAMES=20`。生产默认 2000 帧 / 2MB，
   普通回答挤不爆，`after=0` 走的是增量重放分支，sync 永远不会出现
   （在 8000 上实测回放了 10963 字，就是这个分支）。
2. **续看时 job 还活着** —— job 结束即从 `_JOBS` 摘表，buffer 随之消失，
   `/stream` 直接回 `done`，根本走不到 sync 分支。

第 2 条踩过两次，两次都长得像「机制坏了」：

| 版本 | 做法 | 结果 |
|---|---|---|
| v1 | 断开后 `sleep(6)` 再重连 | `status=complete`，重连拿 `事件 ['done']，正文 0 字，sync 0 帧，seq=0` |
| v2 | 收到 100 字 → `sleep(2)` → 重连 | 8000 上仍 `status=complete`（2 秒写了 5699 字） |
| v3 | **收到 400 字立刻断开并重连，不 sleep** | 8000 上 `status=streaming`（缓冲够大 → SKIP 合理）；8124 上 `status=streaming` → `event: sync`、`blocks`、`seq=113`，**PASS** |

结论：**别用固定 sleep 去「攒帧」**，那轮多快写完取决于模型和 prompt，不可控。
读到足够字就立刻断，缓冲溢出不靠时间攒、靠帧数。

> 这也说明三态设计是必要的：环境不满足时报 FAIL 会让人去查一个不存在的缺陷。

### 崩溃恢复

| 文件 | 验什么 |
|---|---|
| `probe_crash_resume.py` | 跑到 `code_run` 中间被 kill -9，重启后**不重放**那个工具（分 `before` / `after` 两阶段跑） |
| `probe_job_shutdown.py` | 进程被硬杀后，重启时 turn job 的遗留被正确收敛 |
| `probe_persistence.py` | 构造实测：重启到底会丢什么（R1~R4，**只读，不删数据**） |

`probe_crash_resume.py` 是两阶段脚本，中间要手工 kill：

```bash
cd tools/persistence_verify
python probe_crash_resume.py before    # 提交并等待被掐
# 手动 kill 后端 + 重启
python probe_crash_resume.py after     # 续上一轮，看模型怎么处理
```

## ⚠️ 会写库：默认连 8000 与 `backend/data/app.db`

**这些探针会建用户、发消息、打团队记录。** 默认 `HLZ_PROBE_BASE=http://127.0.0.1:8000`
指向本机正式服务，而 8000 用的是 `backend/data/app.db`。

**该库是本地开发库，不是生产数据**（用户 2026-11-15 确认），所以直接在上面跑
测试是可以的 —— 但它**不等于可销毁**：里面仍有真实历史（129 用户 / 173 会话 /
189 团队任务 / 记忆库）。

所以规矩是：

- 跑探针前先问「这个探针会不会写」。会写 → 跑完按 `user_id`（本次 runId 建的用户）
  精确清理，**判据只能是「备份里没有」**，不能用「字段看着像测试」；
- 清理前用 sqlite backup API 备份（`src.backup(dst)`，WAL 安全，不要 `shutil.copy`）；
- 想完全不碰本库时，指到隔离后端：

  ```bash
  python work/_probe_boot.py                    # 隔离库后端，监听 8123
  HLZ_PROBE_BASE=http://127.0.0.1:8123 \
  HLZ_PROBE_DB=work/probe_verify/app.db \
    python tools/persistence_verify/probe_xxx.py
  ```

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `HLZ_PROBE_BASE` | 后端地址 | `http://127.0.0.1:8000` |
| `HLZ_PROBE_DB` | 读/查哪个 sqlite 库 | `backend/data/app.db` |

全部探针都已读这两个变量（`probe_disconnect` / `probe_job_resume` /
`probe_job_resume2` / `probe_job_shutdown` / `probe_crash_resume` / `probe_persistence`），
换端口 / 换库不用改代码。

## 实测记录（2026-11-15）

| 探针 | 结果 |
|---|---|
| `probe_disconnect.py` | Q1 断开后立刻重发 → **409**，**+1s 后 200**（租约 1 秒自愈）；Q3 被掐断的消息最终 **status=complete / len=7535** —— 断开不影响生成 |
| `probe_multiworker.py` | `workers=2` 稳定起两个 worker（两次复现） |
| `probe_job_resume2.py` @ 8000（默认 2000 帧） | R1/R3/R4 **PASS**，R2 **SKIP** —— 缓冲挤不爆，`after=0` 走了增量重放（回放 10963 字，`seq=390`）。**SKIP 是正确结论，不是失败** |
| `probe_job_resume2.py` @ 8124（`REPLAY_FRAMES=20`） | **R1/R2/R3/R4 全 PASS，连跑两轮**。R2 现场：`status=streaming` → `事件 ['sync']`，`blocks` 1–2 块，`seq=113 / 2136`；R4 双连接各自收到 `['sync','text_delta']` |

### probe_job_resume2 的三态约定

结果为 `PASS` / `FAIL` / `SKIP` 三种。**SKIP 只表示环境不满足，不是失败**：

| 情形 | 判为 | 说明 |
|---|---|---|
| 缓冲溢出、收到 `sync` 且 blocks 非空 | PASS | 降级路径真的走到了 |
| 缓冲溢出、`sync` 载荷不对 | FAIL | 真的有问题 |
| 缓冲**没**溢出、走的是增量重放 | SKIP | 生产默认配置下必然是这种；要验先压缓冲 |

把 SKIP 混成 FAIL 会让人以为「续看坏了」，混成 PASS 就是假通过 —— 两种都错，
所以坚持三态。退出码只看有没有 FAIL。

## 已知限制

- `probe_persistence.py` 会 `subprocess` 起停后端，**跑之前确认没有别的东西占 8000**。
- `probe_crash_resume.py` 的两阶段中间需要人工 kill 后端，不能一键跑完。
