# tools/a2a_verify —— A2A 面真机验证四件套

2026-11-14 全量验证时写的。**与 `tools/memory_verify/` 的分工**：那边验记忆的
文件/并发/锁，这边验 **A2A 调用面**（档位、长任务、配额、跨用户）。

| 脚本 | 证明什么 | 耗时 |
|---|---|---|
| `main_cases.py` | A 档位边界 / B 预取形态 / C 长任务 / D 协议边界 | ~2min |
| `degrade_cases.py` | E 退化路径 / F 写侧档位闸 / G 并发配额 / H 子代理闭世界 | ~5min |
| `quota_probe.py` | 单独诊配额：**串行 vs 并发**，用来区分「没生效」和「竞态」 | ~1min |
| `runtime_pool_probe.py` | `RuntimeHub` 的缓存语义（复用/上限/淘汰），**不需要后端** | ~1s |

## 前置

后端必须以 `work/_launch_verify.py` 启动（选择器 + 自动提取 + A2A 全开）。
`work/` 不入库，那个启动器的内容见 `docs/20261114-全量真机验证与A2A并发配额缺陷.md` §5。

```powershell
python work/_launch_verify.py
cd backend
python ../tools/a2a_verify/main_cases.py          # 全部；或 main_cases.py A B
python ../tools/a2a_verify/degrade_cases.py
python ../tools/a2a_verify/quota_probe.py
python ../tools/a2a_verify/runtime_pool_probe.py  # 不需后端
```

## `quota_probe.py` 为什么值得单独留

它回答的是一个**没法用单测回答**的问题：配额失效到底是「逻辑没写对」还是
「并发竞态」。

判据很干脆：

- **串行出现 429，并发没有** → 逻辑对、竞态错（2026-11-14 就是这种，TOCTOU）；
- **串行也没 429** → 配额根本没生效（读不到活跃数 / 上限读错）。

这两种的修法完全不同，混在一起排查会走弯路。

## `runtime_pool_probe.py` 为什么用假实现

它要回答的是「缓存**结构**」问题（有没有上限、有没有淘汰），不是「build 有多贵」。
真实 `build` 会连 MCP、建 httpx client，跑几十个用户要好几分钟，而这些与问题无关。
所以在本进程内把 `UserRuntime` 换成一个只记账的假类 —— **只读，不改源码**。

结论（2026-11-14）：有复用、有并发去重，**没有上限 / 淘汰 / LRU / 空闲超时**，
200 个新用户 → `_runtimes` 202 条（实测）。准确说法是「惰性创建的进程内缓存」，
不是池子。

## 已知的终端噪声

Windows GBK 控制台打印某些字符会 `UnicodeEncodeError`。脚本已 `reconfigure`
成 utf-8 并避开非 ASCII 符号；若仍看到乱码，是控制台编码问题，不影响判定。
