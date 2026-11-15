# turn_job_verify — 生成/连接解耦、断线续看、上下文压缩、页脚结算

> 覆盖「持久化第三步」（TurnJob）与上下文压缩这条线的全部真机验证。
> 2026-11-15 从 `work/` 迁入 `mutate_job_phase1/2`、`probe_multiworker`、`probe_loop_cost`。

## 一、启动器

| 文件 | 用途 |
|---|---|
| `launch.py` | 通用启动器（可指定 `--port` / 隔离库） |
| `launch_diag.py` | 诊断用：把重放缓冲压小，让 sync 降级路径确定性触发 |

## 二、生命周期与帧层

| 文件 | 验什么 |
|---|---|
| `lifecycle_probes.py` | 四组与 CPython 任务/事件循环行为有关的实测（任务建好后未执行就被 cancel、`done_callback` 是否仍触发等）—— 这些结论**不能靠读文档断言** |
| `active_probe.py` | 活 job 状态探针 |
| `heartbeat_http.py` / `heartbeat_prod.py` | SSE 心跳：静默多久会断（实测直连能撑 36s，前面挂 nginx 60s 就断） |
| `mutate_heartbeat.py` | 心跳变异测试 |
| `probe_multiworker.py` | 多 worker（`workers=2`）下的行为；与 `multi_process_lock.py` 互补 |
| `probe_loop_cost.py` | `AgentLoop` 构造开销（回答「为何必须每轮新建」） |
| `mutate_job_phase1.py` | 阶段 1 变异测试：把实现改坏，确认 10 条测试真能抓到 |
| `mutate_job_phase2.py` | 阶段 2 变异测试：重放 / sync 逻辑改坏，确认测试真能抓到 |

## 三、断线续看（真机）

| 文件 | 验什么 |
|---|---|
| `live_selftest.py` | 帧层自测：心跳 / seq / 重放缓冲 / sync 降级 |
| `resume_collab.py` | 真实协作任务 × 断线续看（三场景 A/B/C，各自独立 run 目录） |
| `ui_resume.py` | Playwright：刷新后能否接着看 |

> `resume_collab.py` 的 `apply_resume_frames()` 复刻了**前端真实算法**。
> 第一版断言「首个续看帧必须是 `sync`」是错的：生产默认缓冲 2000 帧，
> `after=0` 几乎总是走增量重放，`sync` 只是兜底。

## 四、上下文压缩与页脚

| 文件 | 验什么 |
|---|---|
| `compact_check.py` | `/compact` API 基础行为（消息少时的中文说明、压缩后可继续发送） |
| `compact_deep.py` | 短会话压缩：摘要进上下文、**哨兵可复述**、连续压缩不毁历史 |
| `compact_long.py` | 长会话压缩：token 必须下降、压缩比 < 0.8 |
| `autocompact_150k.py` | 自动压缩是否在超窗之前触发（`--window` 可调，实测 150k / 30k） |
| `overwindow_behavior.py` | **>100% 窗口期里还能不能发消息**（W-1~W-4） |
| `deferred_compact_live.py` | 停手不发消息时，后台软线压缩能否自己跑完（C-1~C-3，核对 `_source`） |
| `ctx_settle_ui.py` | Playwright：页脚夹取 100%、「整理中…」、压缩后自动结算（G-1~G-7） |
| `ui_acceptance.py` | 挑剔用户视角 21 项：卡顿 / 多功能 / 压缩 / UX |
| `context_probe.py` / `context_big.py` | 抓前端收到的 `/context` 原始响应，与页脚对照 |

### 压缩这条线的判据陷阱（都踩过）

1. **`_source` 的值是 `"summarization"`，不是 `"summary"`**（见 `app/tokens.py`）。
   写错会得出「没有摘要」的假缺陷。
2. **不能只看 `used_tokens` 下降** —— 硬线裁剪也会让它降。必须核对历史里是
   `summarization`（软线摘要）还是 `hard_trim`（硬裁）。
3. **不能只断言「页脚 ≤100%」** —— 充不到软线时也成立，是空断言。
   要「后端确实报过 >100%」且「页脚仍不显示 >100%」。
4. **断言前必须等后台压缩**：赛后压缩是 fire-and-forget，`stream()` 一返回就读
   `/context` 会读到压缩前的值（`autocompact_150k.py` 因此假失败过一次）。
5. **页脚文字有中文前缀**，直接 `float(f.split(" / ")[0])` 会抛 `ValueError`，
   被 `except: continue` 吞掉后「到 85% 停手」永远不生效。用正则取三段数字。

## 五、测试数据清理

| 文件 | 用途 |
|---|---|
| `cleanup_test_accounts.py` | 按时间戳清理测试账号（`--dry-run` 先看） |
| `restore_from_backup.py` | 从备份逐行对齐恢复（**不要猜关联字段做级联删除**，见脚本内注释） |
| `stale_probe.py` / `stale_raw.py` | 「停止被误标」调查工具；`stale_raw.py` 用只读 sqlite URI 连接，避开可疑的 API 路径 |
| `pid_alive_check.py` | 进程存活判定 |

> `stale_probe2.py` 已删除：它通过**可疑的写路径**读状态，
> 观测器本身把现象造了出来（本项目反复出现的错误类别）。

## 六、相关文档

- `docs/20261115-上下文用量超100%的成因与自动压缩时机核实.md`（压缩时机 + 三处 UX 修复）
- `docs/20261115-挑剔用户UI验收-卡顿多功能压缩与UX.md`
- `docs/20261115-生成与连接解耦与断线续看-持久化第三步.md`
- `docs/20261112-Hlaudz首帧串行阻塞核实与优化设计.md`
