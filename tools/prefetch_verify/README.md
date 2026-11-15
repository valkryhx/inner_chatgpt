# prefetch_verify — 记忆预取（M3.5 选择器）探针

> 从 `work/` 迁入（2026-11-15）。这一组验的是「LLM 相关性选择器」这条**默认关闭**的路径，
> 以及它和关键词注入的**互斥**关系。单测只证明函数返回值，不证明真实注入块长什么样。

## 前置

预取默认关闭，需要显式开。`tools/persistence_verify/launch_verify.py` 已经设好：

```bash
cd tools/persistence_verify && python launch_verify.py
```

关键环境变量（见 `docs/20261114-记忆系统M3.5选择性召回实施记录.md`）：

| 变量 | 值 | 为什么 |
|---|---|---|
| `HLZ_MEMORY_SELECTOR` | `1` | 总开关 |
| `HLZ_MEMORY_SELECTOR_MODEL` | 湛卢模型名 | **不填则 `prefetch_enabled()` 恒 False**，等于没测 |
| `HLZ_MEMORY_SELECTOR_MAX_CALLS` | `3` | 每代调用上限 |
| `HLZ_MEMORY_SELECTOR_TIMEOUT_S` | `8` | 选择器超时 |

## 脚本清单

| 文件 | 验什么 | 要点 |
|---|---|---|
| `probe_selector_env.py` | 最轻量：打印环境变量与 `prefetch_enabled()` 的真实结果 | 排查「为什么选择器没跑」的第一步 |
| `probe_selector_client.py` | `selector_client_for(uid)` 到底返回什么 client | 三级兜底（用户配置 → 全局 `llm.yaml` → 显式 fallback，**不自动回落主模型**） |
| `probe_slug_dedup.py` | 注入器与预取**是否共用同一个 slug 集合** | 2026-11-14 真机抓到的缺陷：不共用会导致同一条记忆注入两遍 |
| `probe_prefetch_live.py` | 在真实进程环境里看 `prefetch_enabled` 与 client 的实际结果 | 与 `probe_selector_env.py` 互补：那个看函数，这个看进程 |

## 与关键词路径的互斥（重要）

设计上两者**不叠加**：选择器开启时，`keyword_injection_enabled()` 让位。
叠加等于让预取白做（关键词在首轮模型调用前同步跑，必定先抢走 slug），
还会让用户误以为选择器在起作用。见 `docs/20261114-记忆系统M3.5…md` §3.7。

验证互斥要跑 `tools/memory_verify/verify_today.py` 的 B 组。

## 相关文档

- `docs/20261114-记忆系统M3.5选择性召回实施记录.md`（机制、两个挂点、12 项构造上的坑）
- `docs/20261114-记忆系统M1-M3实施记录.md`（底座）
