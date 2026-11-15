# memory_verify — 记忆系统验证套件

> 2026-11-15 扩充：从 `work/` 迁入 6 个探针（`verify_today` / `verify_degrade` /
> `reverse_m4b` / `sm6_selector_e2e` / `dream_timeline` / `probe_store_roundtrip`）。

## 一、原有七件套（2026-11-14）

见 `docs/20261114-记忆系统三项未覆盖验证记录.md`。

| 文件 | 验什么 |
|---|---|
| `ui_dream.py` | Playwright UI：设置页 dream 区块 52 项断言、8 张截图 |
| `concurrent_write.py` | 进程内并发 6 场景（**抓出 `memory_delete` 无版本保护导致的数据丢失**） |
| `multi_process_lock.py` | 跨进程锁 4 场景（两个真实进程，持锁窗口两两不重叠） |
| `dream_mutation.py` | dream 变异测试（11 条变异必须全被抓） |
| `prefetch_mutation.py` | 预取变异测试（13 条变异必须全被抓） |
| `selector_bench.py` | 选择器基准：关键词 top5 vs 选择器（实测 62.5% → 100%） |
| `run_all.py` | 统一入口 |

## 二、2026-11-15 迁入的探针

这一批是 M5（A2A 档位）与 M3.5（选择器）落地当天的真机验证，
**验的是「档位闸装在哪几个点」这种单测覆盖不到的链路问题**。

| 文件 | 验什么 | 关键背景 |
|---|---|---|
| `verify_today.py` | 四组全量：A 档位边界 × 记忆读写、B 预取注入形态、C A2A 长任务默认窗口、D A2A 协议边界 | 跑法：`python verify_today.py [A B C D]` |
| `verify_degrade.py` | 第二轮专打**坏路**：E 选择器失败/超时、F 写侧档位真实形态、G 并发配额 429、H 子代理拿不到记忆工具 | 第一轮验正常路径，这轮验退化 |
| `reverse_m4b.py` | 反向确认：把实现改坏，确认测试真能抓到（自动改源码 → 跑 pytest → 还原） | 每次输出 `RED(正确)` / `GREEN(漏了!)` |
| `sm6_selector_e2e.py` | S-M6/S-M7 真实链路：走主对话 SSE，验证选择器在路上能注入 | 不是直接调函数 |
| `dream_timeline.py` | 追查截图那一刻页面的 dream 状态来自哪里（每一步同时打印 DB 与页面文本） | 专治「截图是上次残影还是真跑了」 |
| `probe_store_roundtrip.py` | `memory_store` 在临时库里的写-列表-读往返 | 最轻量的冒烟，排查存储层问题先用它 |

## 三、跑之前要什么

- **真实 LLM**（湛卢）。这些脚本得到的结论属于 L3，不能用假 LLM 代替。
- 大部分要后端以 `tools/persistence_verify/launch_verify.py` 启动
  （它把 `HLZ_MEMORY_*` 与 `INNER_A2A_ENABLED` 都设好）。
- `ui_dream.py` 需要 Playwright + Chromium。

## 四、判据纪律（这几轮反复踩过的）

1. **只看 `used_tokens` 下降**会被「删光历史」骗过 —— 必须核对摘要消息真的进了历史
   （`_source == "summarization"`，注意值不是 `summary`）。
2. **「零写入」必须配正向对照**：没有「full token 会写」的对照，
   「read_only 零写入」也可能只是空断言（见 S-M4b 与 S-M4c 那一对）。
3. **反向确认不是可选项**：改完之后把实现改回坏的样子，测试必须变红。
   `reverse_m4b.py` 与 `*_mutation.py` 就是干这个的。
