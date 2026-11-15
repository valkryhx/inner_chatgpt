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

### 断线（最容易搞错的一组，按证据强度递进）

| 文件 | 验什么 | 判据的演进 |
|---|---|---|
| `probe_disconnect.py` | 断开 SSE 后生成还在不在跑 | 第一版：看响应文本里有没有 "generation" —— **判据是坏的**，正常回复也可能出现该词 |
| `probe_disconnect2.py` | 精确版：停没停 | 缩短窗口，仍靠文本 |
| `probe_disconnect3.py` | 「跑完了」还是「跑到一半被掐」 | 比对落库字数与句子完整性 |
| `probe_disconnect4.py` | 断开后能不能立刻重发 | **看 HTTP 状态码**（200 / 409），不猜文本 ← 结论以这版为准 |

> 保留四版是有意的：它记录了「怎么从坏判据走到好判据」。
> 要复跑只需 `probe_disconnect4.py`。

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

## 已知限制

- `probe_disconnect*.py` 硬编码 `BASE = "http://127.0.0.1:8000"`；换端口要改脚本顶部。
- `probe_persistence.py` 会调用 `subprocess` 起停后端，**跑之前确认没有别的东西占 8000**。
