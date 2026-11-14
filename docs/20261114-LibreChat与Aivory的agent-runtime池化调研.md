# LibreChat / Aivory 的 agent runtime 池化机制调研

> 日期：2026-11-14　状态：调研完成（只读源码，未改任何东西）
> 起因：Hlaudz 的 `RuntimeHub` 实测是「惰性缓存，无上限无淘汰」（200 用户 → 202 条常驻），
> 想看看两个成熟项目怎么处理同一类问题。
> 被测版本：LibreChat `f4154b649`、Aivory `adeb4a34`

---

## 0. 先纠正一个前提：两个项目都**没有**「agent runtime 池」

我按「池 = 预分配 + 复用 + 上限 + 淘汰」去搜，结论是**三家都不做预分配**。
但它们各自解决了 Hlaudz 没解决的那一半问题（**上限与淘汰**），而且解法完全不同，
这比「抄一个池」更有参考价值。

| | Hlaudz | LibreChat | Aivory |
|---|---|---|---|
| 被缓存的是什么 | `UserRuntime`（httpx client + MCP 连接 + registry + loop） | **job 状态**（不是 runtime 对象） | **事件流**（不是 runtime 对象） |
| 缓存键 | `(user_id, model_id)` | `streamId`（≈ 一次生成） | `messageID` |
| 上限 | ❌ 无 | ✅ `maxJobs = 1000` | ✅ per-user `16` 条 SSE 连接 |
| 淘汰 | ❌ 无（只有显式 invalidate） | ✅ `evictOldest()`（最老 createdAt） | ✅ 关掉最老连接的 `done` chan |
| 过期 | ❌ 无 | ✅ `ttlAfterComplete` + 60s 定时 `cleanup()` | ✅ Redis TTL 2h |
| 存储 | 进程内 dict | 进程内 dict / **Redis** 可切 | **Redis**（内存仅 dev 回退） |

**最本质的差别**：LibreChat 和 Aivory 都**不缓存「runtime 对象」**。
它们缓存的是**可序列化的状态**（job 状态、事件流），所以天然可以放 Redis、
天然有 TTL、天然能跨副本。Hlaudz 缓存的是**活的连接对象**（httpx client、MCP session），
这种东西没法序列化、没法跨进程、TTL 到期还得正确 close —— 这才是它难做上限的根本原因。

---

## 1. LibreChat：按 job 生命周期管，有硬上限 + TTL + 定时清理

### 1.1 结构

`packages/api/src/stream/GenerationJobManager.ts`（9889 行）是总装层，
真正的存储是 `IJobStore`，有 `InMemoryJobStore` / `RedisJobStore` 两个实现。

关键：`GenerationJobManager` 里有两套东西

```ts
private runtimeState = new Map<string, RuntimeJobState>();   // 运行期状态（AbortController 等）
private ownedJobs = new Map<string, number>();               // 本副本拥有的 job
// 真正的持久层是 this.jobStore（可换 Redis）
```

`runtimeState` 里放的是 `AbortController`、订阅者租约、启动遥测 —— 全是
**不可序列化但可重建**的东西。可序列化的部分（状态、内容、检查点）都在 `jobStore`。

### 1.2 上限：`maxJobs = 1000`，满了淘汰最老的

```ts
// InMemoryJobStore.ts:494
if (!this.jobs.has(streamId) && this.jobs.size >= this.maxJobs) {
  await this.evictOldest();
}
```

```ts
// :1395
private async evictOldest(): Promise<void> {
  let oldestId: string | null = null;
  let oldestTime = Infinity;
  for (const [streamId, job] of this.jobs) {
    if (job.createdAt < oldestTime) { oldestTime = job.createdAt; oldestId = streamId; }
  }
  if (oldestId) {
    logger.warn(`[InMemoryJobStore] Evicting oldest job: ${oldestId}`);
    const job = this.jobs.get(oldestId);
    if (!job) return;
    this.parkQueuedSteers(oldestId, job, Date.now());   // ← 淘汰前先把排队指令存档
    await this.deleteJob(oldestId, job.createdAt);
  }
}
```

三个细节值得注意：

1. **淘汰前先 park**。被淘汰的 job 上排队的用户指令会被存下来（`parkedSteers`，
   TTL 5 分钟），不是直接丢。这是「淘汰不能丢用户数据」的体现。
2. **`createdAt` 不是 LRU**。它淘汰最**早创建**的，不是最久未访问的 ——
   因为 job 是有生命周期的，早创建的更可能已结束。
3. **淘汰在 create 时刻同步触发**，不是等定时清理。满了立刻腾位置。

### 1.3 过期：`ttlAfterComplete` + 60 秒定时 cleanup

```ts
// GenerationJobManager.ts:1020
this.cleanupInterval = setInterval(() => { this.cleanup(); }, 60000);
if (this.cleanupInterval.unref) this.cleanupInterval.unref();   // ← 不阻止进程退出
```

`cleanup()` 做四件事：

1. `failStalePausePersistenceBarriers()` —— 卡死的 pause 写入必须失败关闭，
   不能因为租约过期就变成「可恢复」；
2. `expireStaleApprovals()` —— 过期审批发终态事件，避免客户端连接挂着不动；
3. `jobStore.cleanup()` —— 按 TTL 删已结束的 job；
4. 遍历 `runtimeState`，为已删 job 回收运行期状态（abort、停租约、清 buffer、清 transport）。

**注意 `unref()`**：定时器不阻止进程退出。这是个容易被忽略的细节 ——
忘了它，Node 进程会因为一个 cleanup 定时器而无法优雅退出。

### 1.4 `cleanup()` 里最值得学的：**不误杀正在收尾的东西**

```ts
const isRetainedTerminal =
  currentJob?.createdAt === observedRuntime.createdAt &&
  currentJob.status !== 'running' &&
  currentJob.status !== 'requires_action';
if (!isRetainedTerminal || this.runtimeState.get(streamId) !== observedRuntime) {
  continue;
}
```

三重条件：① 是同一次生成（`createdAt` 相等）；② 已终态；③ 运行期对象还是同一个。
还有更细的：`terminalPersistencePending` / `terminalHostActionPending` /
`providerDrained === false` 三种情况下**跳过**，因为它们还欠着事没做完。

以及这段防御：

```ts
// A replacement generation can reuse the same streamId while hasJob() is in flight.
// Never reap the replacement runtime based on the stale absence observed for its predecessor.
if (this.runtimeState.get(streamId) !== observedRuntime) { ... continue; }
```

`hasJob()` 是 `await` 的，等待期间同一个 `streamId` 可能已经被新一代复用。
如果不比对 `createdAt` / 对象身份，就会**误杀新一代**。

**这是 `await` 与回收交错的经典陷阱**，Hlaudz 的 `RuntimeHub.invalidate` 也有同类风险
（`invalidate` 里 `await ur.close()` 期间，用户可能正在建新 runtime）。

### 1.5 各种 TTL 常量（都能配）

| 常量 | 值 | 用途 |
|---|---|---|
| `PARKED_STEERS_TTL_MS` | 5 min | 淘汰/中断时暂存的用户指令 |
| `STEER_RECEIPT_TTL_MS` | 20 min | 插话回执 |
| `TERMINAL_PERSISTENCE_RETENTION_MS` | 5 min | 终态但还没落库 |
| `HOST_ACTION_RETENTION_MS` | **24 h** | 终态但宿主钩子还没确认（有界窗口，防止永久失败的钩子泄漏） |
| `IDEMPOTENCY_TTL_SECONDS` | 25 h | 幂等键 |
| `SUBSCRIBER_LEASE_TTL_MS` | 30 s | 订阅者租约 |

---

## 2. Aivory：per-user 硬上限 + Redis TTL

### 2.1 SSE 连接池：per-user 16 条，超出踢最老的

```go
// server/internal/api/events_handlers.go:44
eventsMaxConnsPerUser = envcfg.Int("AIVORY_API_EVENTS_MAX_CONNS_PER_USER", 16)

// :127
func (h *eventsHubT) registerForGroup(userID, groupID string) *eventsConn {
	conn := &eventsConn{ch: make(chan string, eventsConnBuffer), done: make(chan struct{}), ...}
	h.mu.Lock()
	defer h.mu.Unlock()
	list := h.conns[userID]
	// Per-user cap: evict the oldest stream rather than rejecting the newest tab.
	for len(list) >= eventsMaxConnsPerUser && len(list) > 0 {
		close(list[0].done)
		list = list[1:]
	}
	h.conns[userID] = append(list, conn)
	return conn
}
```

设计要点：

1. **淘汰最老的，不拒绝最新的**。注释写得很直白：「evict the oldest stream rather
   than rejecting the newest tab」。用户开了第 17 个标签页，不该让它失败，
   该让第 1 个静默重连。
2. **淘汰 = `close(conn.done)`**，不是硬删。消费端 `select` 到 `done` 就优雅退出。
3. **有界 buffer + 满就丢**：`eventsConnBuffer = 16`，`select { case ch <- payload: default: }`。
   丢事件是可接受的，因为「客户端重连后重新拉取」——**best-effort 投递 + 客户端补偿**。

### 2.2 事件流：Redis Stream + 2h TTL

```go
// server/internal/genstream/genstream.go:13
var TTL = 2 * time.Hour

func Key(messageID string) string { return "gen:" + messageID }
func Topic(messageID string) string { return "gen:" + messageID + ":notify" }
func RevocationKey(messageID string) string { return "gen:" + messageID + ":revoked" }
```

用法是 `c.StreamAppendIfAllowed(Key(messageID), denyKeys, payload, TTL)` ——
**原子地检查「是否被撤销」+ 追加**，撤销用 tombstone key。

### 2.3 生产用 Redis，内存只做 dev 回退

```go
// server/cmd/api/main.go:73
if cfg.RedisURL != "" {
    rc, err := cache.NewRedis(cfg.RedisURL)
    ...
    cacheLayer = ...
} else {
    cacheLayer = cache.NewMemory()
}
```

**关键**：内存实现 `memory` 只有惰性 TTL 过期（`Get` 时发现过期才删），
**没有上限、没有主动清理**。但这不是缺陷 —— 它的定位是「本地开发/测试」，
生产走 Redis（Redis 自己有 `maxmemory-policy`）。

作者甚至在注释里写明「Tuned to be simple, not fast — we expect single-digit ops/sec
from the dev profile」。**明确划定了实现的能力边界**，这点值得学。

### 2.4 一个反面对照：Aivory 自己也有「只增不减」的映射

同一个项目里，`server/internal/tools/builtins.go:1167`：

```go
// convSandboxMu serialises sandbox-session provisioning per conversation ...
var convSandboxMu sync.Map // convID -> *sync.Mutex

func lockConvSandbox(convID string) func() {
	m, _ := convSandboxMu.LoadOrStore(convID, &sync.Mutex{})
	mu := m.(*sync.Mutex)
	mu.Lock()
	return mu.Unlock
}
```

**没有删除、没有上限、没有 TTL** —— 每个聊过代码的会话永久留下一个 `*sync.Mutex`。

为什么这在这里**可以接受**而 Hlaudz 的 `_runtimes` 不行？**单条成本差三个数量级**：

| | 单条成本 |
|---|---|
| `convSandboxMu` | ~几十字节（一个空 mutex） |
| Hlaudz `_runtimes` | httpx client + MCP session + registry + loop，**KB 级且持有套接字** |

所以「有没有上限」这件事不能脱离**单条成本**谈。Aivory 在这个位置省掉上限是合理的
工程判断；Hlaudz 在 runtime 这个位置省掉上限就不合理。**这条对照本身是判断依据**，
比单纯「抄别人加了上限」更有用。

---

## 3. 对 Hlaudz 的具体启示

Hlaudz 的问题是：`RuntimeHub._runtimes` 缓存**活的连接对象**，无上限、无淘汰、
无空闲过期。参考上面两家，有三条可选路线：

### 路线 A（最直接）：照 LibreChat 加「上限 + 淘汰最老」

```python
MAX_RUNTIMES = 200          # 可配
if key not in self._runtimes and len(self._runtimes) >= MAX_RUNTIMES:
    await self._evict_oldest()
```

**但有一个 LibreChat 不需要考虑、Hlaudz 必须考虑的差别**：LibreChat 淘汰的是
**已结束的 job 状态**，Hlaudz 淘汰的是**可能正在用的 runtime**。
必须加「活跃轮次不淘汰」的闸 —— Hlaudz 已有现成的
`has_active_generation_for_user(user_id)`（`refresh_mcp_status` 里就在用）。

淘汰顺序建议：先挑已结束且最久未用的，实在没有可淘汰的才**拒绝新建**（返回明确错误），
**绝不能杀掉正在跑的**。

### 路线 B（最彻底，但工程量大）：把 runtime 拆成「可序列化状态 + 可重建连接」

学 LibreChat 的 `jobStore` / `runtimeState` 分离，以及 Aivory 的全 Redis 化。
Hlaudz 的 `httpx.AsyncClient` 和 MCP session 都能重建，但重建代价是秒级
（MCP 要重新握手）。这条路能让 Hlaudz 支持多 worker，但要重设计整个 runtime 层。

**暂时不值得做** —— 单 worker 部署目前够用。

### 路线 C（最小改动，先止血）：加空闲过期

比 A 简单：记录 `last_used_at`，定时扫描，空闲超过 N 分钟的 drop 掉。
代价是不会限制「同时活跃」的数量，只限制「长期不活跃」的。对付「用户越用越多、
但每个用户不是每秒都在用」这个真实分布最有效。

### 我认为的优先级

1. **先加空闲过期（C）** —— 改动最小，解决 90% 的实际增长；
2. **再加硬上限（A）** —— 防「大量用户同时活跃」的极端情况；
3. **不做 B** —— 除非真的要上多 worker。

### 另外两个可以直接抄的点

**① 定时器要 `unref`（对应 Python 的 daemon 语义）**
LibreChat 的 `this.cleanupInterval.unref()`。Hlaudz 的 dream 巡检任务挂在 `lifespan`，
如果有类似的周期任务，要确认它不会阻止进程退出。

**② `await` 与回收交错的防误杀**
LibreChat 用 `createdAt` 比对 + 对象身份比对，防止「`await hasJob()` 期间同一个
`streamId` 被新一代复用，回来把新一代杀了」。

Hlaudz 的 `RuntimeHub.invalidate()` 是这样的：

```python
for key in list(self._runtimes.keys()):
    if key[0] == user_id:
        ur = self._runtimes.pop(key, None)   # 先摘
        ...
        if ur is not None:
            await ur.close()                  # 再关
```

**先 pop 再 close** 这个顺序是正确的（新请求不会拿到正在关闭的对象），
但**用户如果在 `await ur.close()` 期间发新请求**，会走到 `get_user_runtime`
重新 build —— 这在语义上是对的（新 runtime），只是会有一瞬间两个 runtime 并存。
不算 bug，但如果将来加淘汰逻辑，这个交错点要特别小心。

---

## 4. 两家的取舍差异（一句话总结）

| | 关注点 | 核心手法 |
|---|---|---|
| **LibreChat** | **单次生成的生命周期**（长任务、断线重连、审批暂停） | job 状态机 + 硬上限 1000 + TTL + 60s 定时清理 + 淘汰前 park |
| **Aivory** | **连接数不爆** + **跨副本一致** | per-user 硬上限 16 + 踢最老 + Redis Stream TTL 2h + best-effort 投递 |

**LibreChat 更值得 Hlaudz 借鉴**，因为：

- 它同样面临「一个进程内管理很多活的执行体」这个问题；
- 它的 `runtimeState` 与 `jobStore` 分离，正好对应 Hlaudz「哪些能缓存、哪些必须重建」的疑问；
- 它的「清理时不能误杀正在收尾的」那套条件，直接对应 Hlaudz 的
  「淘汰不能杀正在跑的轮次」。

Aivory 的 per-user 硬上限 + 踢最老也值得抄，但那是**连接**层面的，
和 Hlaudz 的 **runtime 对象**层面还隔一层。

---

## 5. 证据索引（便于复核）

| 结论 | 文件:行 |
|---|---|
| LibreChat `maxJobs = 1000` | `packages/api/src/stream/implementations/InMemoryJobStore.ts:206` |
| 满了淘汰最老 | 同上 `:494`、`:1395` |
| 淘汰前 park 排队指令 | 同上 `:1412` |
| 60s 定时 cleanup + unref | `packages/api/src/stream/GenerationJobManager.ts:1020-1026` |
| 清理时三重条件防误杀 | 同上 `:9519-9531` |
| `await hasJob()` 期间防误杀新一代 | 同上 `:9547-9555` |
| 各 TTL 常量 | `InMemoryJobStore.ts:54-63` |
| LibreChat 可换 Redis | `packages/api/src/stream/implementations/RedisJobStore.ts` |
| Aivory per-user 上限 16 | `server/internal/api/events_handlers.go:44` |
| 踢最老的连接 | 同上 `:127-140` |
| 有界 buffer 满就丢 | 同上 `:47`、`:171-174` |
| 事件流 TTL 2h | `server/internal/genstream/genstream.go:13` |
| 生产 Redis / dev 内存 | `server/cmd/api/main.go:73-80` |
| 内存缓存无上限（dev only） | `server/internal/cache/cache.go:73-88` |
| Aivory 自己的只增不减映射（反面对照） | `server/internal/tools/builtins.go:1167-1173` |

---

## 6. 变更记录

| 日期 | 内容 |
|---|---|
| 2026-11-14 | 初版。只读调研 LibreChat `f4154b649` 与 Aivory `adeb4a34`，未改动任何代码。结论：两家都不做 runtime 对象池化，都靠「可序列化状态 + 上限 + TTL」；Hlaudz 缓存活对象是根本差异，建议先加空闲过期、再加硬上限，不做全 Redis 化 |
