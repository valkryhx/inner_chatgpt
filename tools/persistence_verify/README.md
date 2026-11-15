# persistence_verify — 持久化 / 断线续看 / 崩溃恢复 探针

> 从 `work/` 迁入（2026-11-15）。这些探针验的是「进程或连接断了之后，状态对不对」，
> 单测**原理上**测不到 —— 必须真起后端、真断开、真 kill。

## 前置

- 后端**必须**用本目录的 `launch_verify.py` 启动（它压小重放缓冲到 20 帧，
  让「缓冲溢出 → 降级 sync」这条路径在真机上确定性触发；生产默认 2000 帧挤不爆）：

  ```bash
  cd tools/persistence_verify
  python launch_verify.py          # 监听 8000
  ```

- 真实 LLM（湛卢）。`launch_verify.py` 会把记忆选择器等开关打开，
  key 从 `llm.yaml` / 用户配置读，别把 key 写进脚本。

## 脚本清单

### 断线

| 文件 | 验什么 |
|---|---|
| `probe_disconnect.py` | 断开后能不能立刻重发（看 HTTP 状态码，不猜文本） |

验三件事：

- Q1 断开后立刻重发 → 200 还是 409？
- Q2 若 409，等多久能恢复？
- Q3 那条被掐断的 assistant 消息最终是什么状态？

> **判据演进（为什么不再看文本）**：早期版本用「响应文本里有没有 `generation`
> 这个词」判断是否被租约挡住 —— 那个判据是坏的，正常回复里也可能出现该词，
> 而 409 的 body 里更是必然出现，等于恒真。现在只看**状态码**。

### 续看（阶段 1 / 阶段 2）

| 文件 | 验什么 |
|---|---|
| `probe_job_resume.py` | 断开后生成是否继续到自然结束（阶段 1：解耦） |
| `probe_job_resume2.py` | 新连接能否接上正在跑的那一轮（阶段 2：续看，R-1~R-5） |

`probe_job_resume2.py` 的五个场景：

- R-1 新线续看：新开 → 重连 `GET /stream?after=k` → 收到尾部事件
- R-2 游标太老降级 sync：`after=0` 且缓冲被挤压时收到 `event: sync`
- R-3 无活跑 job：已跑完的会话调 `/stream` 返回 `done`（不是 404）
- R-4 双连接同时看：两个连接收到同样的后续内容
- R-5 续看不产生重复消息：`messages` 表行数不变

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

## ⚠️ 先读这条：默认会写生产库

**这些探针会建用户、发消息、打团队记录。** 默认 `HLZ_PROBE_BASE=http://127.0.0.1:8000`
指向正式服务，而 8000 用的是生产库 `backend/data/app.db`。

2026-11-15 我就是在 8000 上直接跑了 `heartbeat_prod.py` 与 `stale_raw.py`，
往生产库写入了 3 个测试用户 + 33 行关联数据（已按 `user_id` 精确清理，
25 张表重新 `ALL MATCH`）。

**规矩**：跑之前先问自己「这个探针会不会写」。会写 → 必须指向隔离后端：

```bash
python work/_probe_boot.py                    # 隔离库后端，监听 8123
HLZ_PROBE_BASE=http://127.0.0.1:8123 HLZ_PROBE_DB=work/probe_verify/app.db python tools/persistence_verify/probe_xxx.py
```

只有明确要验「生产配置是否生效」（如 `heartbeat_prod.py` 验 `.env` 的 15s 心跳）
才该连 8000，且跑完要核对库并清理。

## 环境变量（2026-11-15 新增，为了不在生产库上跑）


这些探针会**写会话**（建用户、发消息）。默认指向生产库 `backend/data/app.db`，
所以默认值只适合「就是要验生产」的场合。常规验证请指到隔离库：

| 变量 | 作用 | 默认 |
|---|---|---|
| `HLZ_PROBE_BASE` | 后端地址 | `http://127.0.0.1:8000` |
| `HLZ_PROBE_DB` | 读/查哪个 sqlite 库 | `backend/data/app.db` |

做法是起一个隔离库后端，再带上这两个变量：

```bash
# 1) 起隔离后端（复制生产库做副本，端口不同）
python work/_probe_boot.py            # 监听 8123，库在 work/probe_verify/app.db

# 2) 指过去跑
HLZ_PROBE_BASE=http://127.0.0.1:8123 HLZ_PROBE_DB=work/probe_verify/app.db python tools/persistence_verify/probe_disconnect.py
```

## 已知限制


- `probe_disconnect*.py` 硬编码 `BASE = "http://127.0.0.1:8000"`；换端口要改脚本顶部。
- `probe_persistence.py` 会调用 `subprocess` 起停后端，**跑之前确认没有别的东西占 8000**。
