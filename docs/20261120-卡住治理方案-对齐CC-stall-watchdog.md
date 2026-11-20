# Hlaudz 侧方案：卡住治理（对齐 CC 的 stall watchdog 形状）

日期：2026-11-20
依据：`claudecode283_hack/docs/20261120-CC319卡住治理-stall-watchdog剖析.md`
（33 项坐标已脚本复算）

---

## 0. 两次订正：事故不存在，「前奏慢」也不存在

这份方案的前两版都建立在一个**不存在的问题**上。两次都是同一个错误：
**把中途快照 / 错误字段含义当成结论**。

### 0.1 订正一：第三次运行没卡住

`co_f49ca93ebc14` 的终态是**成功**：`researcher` 28 轮 completed、
`writer` 20 轮 completed、两个产物 51,755 B + 70,389 B、消息 `complete`。
详见 `docs/20261120-AgentTeam修复后e2e验证.md` §10。

错因：探针 900s 超时退出，我在那个时刻查 DB 看到 `running` / `turns=0`
就写了"卡住"。而 `turns` 只在 **LLM 回合结束**时 +1，前奏期间恒为 0。

### 0.2 ★ 订正二：前奏也没慢，`total_ms` 测的是整轮

我据此写了 P0「定位前奏为什么能跑 24 分钟」。**这是错的。**

`prelude.emit()` 在 `_event_stream` 的 `finally` 里（`routers/chat.py:827`），
所以 `total_ms = timer.elapsed_ms()` 是**从进入生成器到整轮结束**，
不是到首帧。真正的前奏终点是 `prelude.mark("first_frame")`（`chat.py:433`），
紧跟在第一个 `yield frame("message_start", ...)` 之后。

现有 6 条 `chat.prelude` 日志的真实分布（全部字段，单位 ms）：

| 会话 | add_msg | runtime | hard | sanitize | build_turn | team | **first_frame** | total_ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `co_1fbecd87ce2a` | 11.5 | **12014.9** | 4.4 | 3.2 | 14.2 | 24.7 | **11.6** | 674,816 |
| `co_690fc1d83c53` | 13.4 | 0.0 | 3.4 | 2.1 | 2.6 | 27.2 | **13.4** | 630,094 |
| `co_8abfb27422c8` | 9.0 | 0.0 | 2.5 | 2.5 | 2.5 | 13.4 | **9.0** | 841,947 |
| `co_4916630a768c` | 13.4 | **12014.0** | 3.4 | 3.9 | 15.9 | 25.3 | **13.6** | 325,750 |
| `co_f5276a34b01b` | 11.8 | 0.0 | 2.8 | 2.6 | 3.1 | 21.6 | **11.8** | 1,073,525 |
| `co_f49ca93ebc14` | 10.8 | 0.0 | 3.2 | 3.0 | 2.7 | 20.5 | **10.8** | 1,465,098 |

**`first_frame` 全部在 9.0–13.6 ms。前奏是毫秒级，从来没有慢过。**
`total_ms` 的 325s / 1073s / 1465s 是整轮（含子代理调研与写作），
与"卡住"无关 —— 那是 agent team 在真干活的时长。

> **这条字段命名本身就是缺陷**：日志文案写「首帧前奏 … total=1465098ms」，
> 任何人（包括我）读到都会以为前奏花了一千多秒。见 §5。

---

## 1. 唯一剩下的真问题：`tavily` 每次冷连吃满 12 秒

`runtime` 那一列是**双峰**：要么 0.0（runtime 已缓存），要么 **12014ms 左右**。
两次非零值精确到毫秒级同值（12014.9 / 12014.0）⇒ 不是网络抖动，
是**一个固定超时被吃满**。

### 1.1 定位

`agent.py:210`：

```python
connect_timeout = float(os.environ.get("MCP_CONNECT_TIMEOUT", "12"))
...
await asyncio.wait_for(ts.__aenter__(), timeout=connect_timeout)
```

逐个真连实测（探针 `work/_mcp_timing_probe.py`，容器内跑）：

| server | 耗时 | tools | 结果 |
| --- | --- | --- | --- |
| `sqlbotmcp` | 201.2ms | 12 | ✅ |
| `falcomm` | 182.0ms | 23 | ✅ |
| `exa` | 2064.6ms | 2 | ✅ |
| **`tavily`** | **12003.9ms** | **0** | ❌ **TimeoutError，吃满 12s** |

**`tavily` 每次冷连都超时，并且一个工具都没拿到。**

### 1.2 为什么日志里完全看不见

`agent.py:251-259` 的 except 分支：

```python
except Exception as e:  # noqa: BLE001
    ...
    return name, None, {
        "name": name, "ok": False, "tool_count": 0,
        "error": f"{type(e).__name__}: {e}", "tools": [],
    }
```

**只把异常写进 manifest，不打日志。** 所以：

```
docker logs ... | grep -c TimeoutError   → 0
```

零命中，但超时每轮都在发生。这是本次排查里最耗时的一环 ——
我先是去查 MCP 端点可达性（4 个全通，反而更困惑），
因为**"端点可达"与"能完成 MCP 握手"是两件事**。

补充实测（`curl` 直接打 initialize）：

| 端点 | HTTP | 耗时 |
| --- | --- | --- |
| `falcomm` | 401 | 0.019s |
| `sqlbotmcp` | 200 | 0.033s |
| `exa` | 200 | 0.966s |
| `tavily` | 200 | **7.038s** |

`tavily` 单次 HTTP 就要 7 秒 —— 它慢是上游事实，不是我们代码的问题。
但**我们把它放进了并发连接，所以它拖住了 `runtime` 整段 12 秒**。

### 1.3 影响面

| 后果 | 说明 |
| --- | --- |
| 每次 runtime 冷启动 +12 秒 | `get_user_runtime` 的 `await ur.build(...)` 阻塞在 `gather` 上 |
| 用户少一个搜索源 | tavily 0 工具，模型看不到它 |
| 却不报错 | manifest 里 `ok=False` 但没人展示、没人告警 |

注意：**这不影响首帧**（首帧在 `runtime` 之前就 yield 了，见 `chat.py:431`），
所以它不是"卡住"，只是"每次冷启动白等 12 秒"。

## 2. 那 stall watchdog 还要不要做

**要，但优先级降到 P2，理由不是这次事故。**

这次三次运行全部成功，没有一个是 watchdog 该介入的。CC 的 watchdog
治的是「子代理跑着跑着不动了」，Hlaudz 目前没复现出这种情况。

`docs/20261120-CC319卡住治理-stall-watchdog剖析.md` §7 的 `must_not` 断言
（CC 没有"整轮总时长超时"）才是关键结论：

> **不应该做"整轮超时"** —— 实测最慢整轮 1465s 且成功，
> 任何按总时长中止的方案都会砍掉正常任务。

watchdog 要按 CC 的形状做（定时器 + 每事件重排 + 工具在飞时 deferred +
结构化 `exitPath`），但**默认只 warn 不 abort**，等观察到真实卡死再切。

---

## 3. 修订后的优先级

| 优先级 | 事项 | 依据 |
| --- | --- | --- |
| **P0** | **修 `tavily` 的 12 秒阻塞** | §1 实测：唯一确凿的缺陷 |
| **P0** | **`_connect_one` 失败必须打日志** | §1.2：超时零日志，排查被它拖了很久 |
| P1 | `prelude` 日志字段改名（`total_ms` → `turn_ms`，另加 `first_frame_ms`） | §0.2：字段名误导，我自己被它误导两次 |
| P2 | stall watchdog（定时器 + 重排 + deferred + `exitPath`），**先 warn** | §2 |
| P3 | `exitPath` 区分 `stopped` 与 `watchdog_stall` | 独立于 P2 也有价值 |
| — | ~~定位"前奏为什么 24 分钟"~~ | **撤销**，前提错误（§0.2） |
| — | ~~整轮总时长超时~~ | **不做**，会砍掉合法运行（§2） |

## 4. P0 具体改法

### 4.1 `_connect_one` 加日志（一行改动，收益最大）

```python
except Exception as e:  # noqa: BLE001
    logger.warning(
        "MCP server 连接失败 name=%s timeout=%.0fs elapsed=%.1fms err=%s: %s",
        name, connect_timeout, elapsed_ms, type(e).__name__, e)
```

没有这条，下次换个 server 慢还会重演同样的排查。

### 4.2 `tavily` 的三种处理（按顺序试）

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| A | 把 `MCP_CONNECT_TIMEOUT` 提到 30s | 每轮再慢 18 秒，**更糟** |
| B | 把 `tavily` 加进"已知慢"豁免名单，给更长超时但**不阻塞别人** | 需要一个 per-server timeout |
| C | 诊断 tavily 为什么 7 秒 —— 可能只是 key 配额/地理位置 | 先查再定 |

**推荐顺序 C → B**。A 不可取：它把"慢"变成"更慢"，而 tavily 现在
**本来就 0 工具**，等更久没有任何收益。

per-server timeout 的形状（若走 B）：

```python
_DEFAULT_CONNECT_TIMEOUT = float(os.environ.get("MCP_CONNECT_TIMEOUT", "12"))
_SLOW_SERVERS = {"tavily": 30.0}      # 或从 db 的 per-server 字段读

timeout = _SLOW_SERVERS.get(name, _DEFAULT_CONNECT_TIMEOUT)
await asyncio.wait_for(ts.__aenter__(), timeout=timeout)
```

> ⚠️ **但即使加了 per-server timeout，`gather` 仍会被最慢那个拖住。**
> 真正的修法是「**软超时**」：先给 12 秒，超时的 server 从 manifest 标
> `pending` 并**在后台继续连**，runtime 先返回已连好的那些。
> 这样 `runtime` 段的上界由"第二慢的 server"决定，而不是最慢的。
> 这是本方案唯一需要动结构的地方，单独一步做。

### 4.3 判据（怎么证明修好了）

| 判据 | 修前 | 修后期望 |
| --- | --- | --- |
| `runtime` 段耗时 | 双峰 0 / 12014ms | **恒 < 3000ms**（exa 是 2s，第二慢） |
| `tavily` manifest | `ok=False, tools=0` 静默 | 要么 `ok=True`，要么有一条 WARNING |
| `grep -c "MCP server 连接失败"` | 0（因为不打日志） | 超时出现时 >= 1 |

**必须用真机跑一轮 agent team 来验**，不能只看单测 ——
这个缺陷的全部特征（12 秒、静默）都在真实连接路径上。

---

## 9. 实施记录（2026-11-20 当晚，已落地）

上面 §0–§8 是方案推演过程，保留以记录判断如何变化。本节只写**实际做了什么**。

### 9.1 已被证伪的前提（不要再引用）

| 原判断 | 实际 | 订正依据 |
| --- | --- | --- |
| 「前奏能跑 24 分钟」 | `first_frame` 恒在 **9–14 毫秒**；`total_ms` 量的是整轮 | 6 条 prelude 日志实测，见 `docs/20261120-多智能体读写慢的定量排查.md` §5 |
| 「第三次运行卡死」 | 它成功了（28+20 轮，2 个产物） | `docs/20261120-AgentTeam修复后e2e验证.md` §10 |
| 「runtime 慢是网络抖动」 | 是 `tavily` 每次冷连吃满 `MCP_CONNECT_TIMEOUT=12` | 逐 server 真连实测 |

### 9.2 实际完成的改动

| # | 改动 | 文件 | 测试 |
| --- | --- | --- | --- |
| 1 | `_connect_one` 失败/零工具必打日志（带 name / elapsed） | `backend/app/agent.py` | `test_mcp_connect_failure_logged.py`（3） |
| 2 | `prelude` 字段改名：`total_ms` → `turn_ms` + 新增 `first_frame_ms` | `backend/app/prelude_timing.py` | `test_prelude_field_naming.py`（3） |
| 3 | 新增动作时间轴 `ActivityTrace`（t=NNNms + 50 字摘要） | `backend/app/activity_trace.py` | `test_agent_activity_trace.py`（8） |
| 4 | 看门狗补 CC 的 deferred 语义（工具在飞不判死） | `backend/app/turn_job.py` | `test_turn_job_watchdog.py`（10） |
| 5 | 时间轴接入 `_event_stream`（含子代理事件） | `backend/app/routers/chat.py` | 同上 |

全部 1,701 项测试通过；每项都做过「移除实现后测试重新变红」的反向确认。

### 9.3 配置侧的两个改动（非代码）

| 改动 | 原因 | 效果 |
| --- | --- | --- |
| `tavily` MCP 配置删除 | 每次冷连 19 秒（connect 6.1s + tools/list 12.9s），预算 12 秒⇒每次都被杀、0 工具 | `runtime` 段从 12,014ms 降到 2,133ms（最慢 = exa） |
| `exa` 换成带 API key 的 URL | 无 key 版本工具少一个 | 工具 2 → 3（多 `web_search_advanced_exa`），耗时 2.0s → 3.5s |

> ⚠️ `tavily` 是**用户配置被删除**，不是代码禁用。原 URL 记在
> `docs/20261120-多智能体读写慢的定量排查.md` 的排查记录里，
> 若将来 tavily 提速可以重新加回。

### 9.4 仍未做的（诚实清单）

| 项 | 为什么没做 |
| --- | --- |
| stall watchdog 的 `abort` 模式 | 实测没有真卡死过，先只做检测（`deferred` 已实现），避免误杀 |
| 结构化 `exitPath` 全量 | 目前只有 `watchdog_fired/reason` 两个字段，够用；`exitPath` 枚举是下一轮 |
| 子代理级 stall watchdog | 本轮实测子代理都在正常推进（21 轮 / 13 轮），未复现停滞 |
| CC 的 `mBi()` rearm 条件 | 未读实现，Hlaudz 无对应概念（没有 "parked" 状态） |
| 前奏分段阈值告警 | **前提不存在**（前奏只有 10ms），不做 |
| 整轮总时长超时 | **明确不做**——会砍掉合法的 1465s 运行 |
