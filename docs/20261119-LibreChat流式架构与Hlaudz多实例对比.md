# LibreChat 流式架构 vs Hlaudz：为什么它不需要粘性会话

> 日期：2026-11-19
> 对象：LibreChat `v0.8.9-rc1`（git `12b5e9023`，`/d/git_codes/LibreChat`）对照 Hlaudz（`feat/sandbox-mvp`）
> 起因：Hlaudz 目前「任意实例接任意流」做不到，需要粘性 LB。问题是 LibreChat 是不是把
> SSE 缓冲外置到 Redis 了？有什么值得借鉴？

---

## 1. 先给结论

**是的，LibreChat 做的正是「把流外置到 Redis」。** 而且它做得比「外置缓冲」更彻底：

| 问题 | LibreChat 的答案 |
| --- | --- |
| 生成跑在 A，客户端连到 B 怎么办 | B 从 Redis 订阅 pub/sub 频道，**不需要**找到 A |
| 晚到的订阅者怎么补上已经发过的事件 | `:seq` 计数器 + `:activity-backlog` 列表，原子 Lua 一次拿快照 |
| 快照与实时事件重叠怎么办 | `READ_REPLAY_LUA` 返回 `frontier` 序号，客户端丢弃 ≤ frontier 的实时帧 |
| pub/sub 投递乱序怎么办 | 订阅侧 reorder buffer，等 seq 连续，最多等 500ms |
| 每条 token 一次 Redis 往返太重 | 25ms 合并窗口，最多 64 帧 / 128KB 一批 |
| 控制面（停止/插话）落在非 owner 实例 | `emitChunkFromAnyReplica` —— 非 owner 走「先落 durable chunk log，再直接 publish」 |

**最关键的一条证据：整个仓库的流式层零粘性依赖。**

```
grep -rniE "sticky|affinity" packages/api/src/stream/          -> 0 命中
grep -rniE "sticky|affinity|ip_hash" docker-compose*.yml nginx* -> 0 命中
```

全仓 `sticky` 的 10 处命中全是「技能粘性」「usage 粘性」「CSS position: sticky」这类无关词，
其中一处还反过来证明它**假设**没有亲缘性：

```ts
// packages/api/src/mcp/oauth/tokens.ts:1296
behind a load balancer without session affinity, one user's concurrent
requests ... land on different replicas
  -> 所以要跨副本加租约（FlowStateManager.acquireLease，Redis Lua CAS）
```

也就是说：LibreChat 把「无亲缘性」当成**设计前提**，凡是需要串行的地方都自己加跨副本锁。
Hlaudz 目前把「有亲缘性」当成**运行前提**，凡是需要跨实例的地方都要绕。这是两套架构假设。

---

## 2. Hlaudz 现状：为什么粘性是正确性依赖

不是「没做优化」，是「异步缓冲在进程内存里」。

```
backend/app/turn_job.py:113    _JOBS: dict[tuple[str, str, str], "TurnJob"] = {}
backend/app/turn_job.py:189    class TurnJob:   # seq / replay deque / subscribers
```

一个 `TurnJob` 持有本轮生成的全部流式状态：

| 状态 | 位置 | 作用 |
| --- | --- | --- |
| `self.subscribers` | `turn_job.py:198` | 当前挂着的 SSE 连接 |
| `self.seq` | `turn_job.py:206` | 单调序号 = 客户端续看游标 |
| `self.replay` | `turn_job.py:208` | `deque[(seq, frame)]`，丢最老，上限 2000 帧 / 2MB |
| `self.blocks_provider` | `turn_job.py:212` | **读「此刻」blocks 的闭包** |

读取侧只有一个入口：

```python
# backend/app/routers/chat.py:1053（_resume_decision 里查进程内 job）
job = turn_job.get_live_job_for_session(user["id"], session_id)
if job is None:
    # 没有活着的 job -> 用库里的现状收尾
```

`get_live_job_for_session` 查的是 `_JOBS`，**进程内字典**。所以请求落到 B 而生成在 A 时，
B 看到 `job is None`，直接走「已完成」分支 —— 用户看到内容停住。

`deploy/nginx.conf:91` 的 `hash $cookie_hlz_sticky;` 就是为了消灭这个场景：同一个浏览器
永远落到同一个上游。这条配置不是性能优化，是**正确性补丁**。

### 已经外置的部分（容易误判成「都做了」）

Hlaudz 的 cluster 层已经有跨实例总线，但方向都是「控制信号」，不是「事件流」：

| 模块 | 方向 | 承载 |
| --- | --- | --- |
| `cluster/stopsignal.py` | 控制 | `SET hlz:stop:<u>:<s>:<g>` + 1s 轮询 |
| `cluster/steerbus.py` | 控制 | `RPUSH hlz:steer:<u>:<s>:<g>` + 200ms 轮询 |
| `cluster/invalidate.py` | 控制 | 缓存失效通知 |
| `turn_job.py` | **数据** | 进程内，**没有** Redis |

而且 stopsignal / steerbus 都是**轮询搬运**（把 Redis 的东西搬进本地队列），不是直连 pub/sub。
这在「用户中途说一句」这种低带宽场景完全够用；但 token 级事件流不能这么干（1s 延迟 = 看不下去）。

---

## 3. LibreChat 的分层：两件东西，四个实现

核心设计是**把「作业状态」和「事件流」拆成两个接口**，各自有内存版和 Redis 版。

```
packages/api/src/stream/
├── interfaces/IJobStore.ts          IJobStoreV2（持久作业状态） + IEventTransport（实时事件流）
├── implementations/
│   ├── InMemoryJobStore.ts          100 KB（2,916 行）
│   ├── RedisJobStore.ts             267 KB（5,987 行）
│   ├── InMemoryEventTransport.ts    13.4 KB（403 行）
│   └── RedisEventTransport.ts       74 KB（1,987 行）
└── createStreamServices.ts          按 USE_REDIS_STREAMS 选实现
```

```ts
// createStreamServices.ts
export interface StreamServices {
  jobStore: IJobStoreV2;
  eventTransport: IEventTransport;
  isRedis: boolean;
}
const useRedis = config.useRedis ?? cacheConfig.USE_REDIS_STREAMS;
```

`USE_REDIS_STREAMS` 不设时跟随 `USE_REDIS`。于是同一份业务代码：单机部署跑内存版，
集群部署跑 Redis 版，**业务侧没有 if**。

**这个拆分本身就是最值得借鉴的一点。** Hlaudz 现在是 `_JOBS` 一个 dict 直接暴露给
`routers/chat.py`，没有中间接口层，所以「换成 Redis」= 改调用方，不是改实现。

### 3.1 `IEventTransport`：实时流

```ts
// interfaces/IJobStore.ts:1724
export interface IEventTransport {
  subscribe(streamId, handlers: { onChunk, onReplay?, onDone?, onError? }, options?: {
    deferSequenceDelivery?: boolean;   // 等 syncReorderBuffer 建立 frontier 再放行
    replay?: ReplayLimits;             // 每订阅者独立的有界重放
    captureSequenceFrontier?: boolean; // 开新频道后原子抓 frontier
  }): { unsubscribe, ready?, syncReorderBuffer? };
  emitChunk(streamId, event, generationId?): void | Promise<void | number>;  // Redis 返回绝对 seq
  emitDone / emitError(...)
  renewDemand?(streamId, ttlMs)        // 「有人在看」标记
}
```

注意 `emitChunk` 的返回类型：`Promise<void | number>` —— Redis 模式下返回**分配的绝对序号**，
冷启动和内存模式返回 void。调用方据此知道「我这帧落在哪个 seq」，重放时才能对齐。

### 3.2 Redis key 布局（hash tag 是为了 Cluster 同槽）

```ts
// implementations/RedisEventTransport.ts:24
const CHANNELS = {
  events: (id) => `stream:{${id}}:events`,          // pub/sub 频道
};
const KEYS = {
  sequence:       (id) => `stream:{${id}}:seq`,              // INCR 分配的全局序号
  backlog:        (id) => `stream:{${id}}:activity-backlog`, // 重放列表
  backlogBytes:   (id) => `stream:{${id}}:activity-bytes`,   // 字节预算
  job:            (id) => `stream:{${id}}:job`,              // 作业 hash（续命用）
  generationEpoch:(id) => `stream:{${id}}:generation-epoch`,
  demand:         (id) => `stream:{${id}}:demand`,           // 有人在看才保留重放
  abortAck:       (id, g) => `stream:{${id}}:abort-ack:${g}`,
  providerDrain:  (id, g, e) => `stream:{${id}}:provider-drain:${g}:${e}`,
};
```

`{streamId}` 花括号是 Redis Cluster 的 hash tag：**保证同一个流的所有 key 落在同一个槽**，
否则那条 Lua 脚本（同时操作 4 个 key）在 Cluster 下会直接报 CROSSSLOT。

Hlaudz 现在用的是 `hlz:stop:<u>:<s>:<g>` 这种散键，单机 Redis 没问题，上 Cluster 就得改。
不过这不是迁移阻塞项 —— 可以在 key 设计时一次到位。

---

## 4. 三个真正的难点，LibreChat 的解法

「把缓冲放进 Redis」听起来是体力活，实际有三个必须同时解决的坑。

### 坑 1：晚到订阅者 —— 先补历史还是先接实时？

两种朴素做法都错：

- 先读快照再订阅 → 快照与订阅之间的事件**永久丢失**
- 先订阅再读快照 → 快照期间收到的事件**重复**

LibreChat 的解法是**先订阅确认、再原子快照、再用 frontier 去重叠**：

```lua
-- internal/replay.ts: READ_REPLAY_LUA
-- SUBSCRIBE 必须先 ack。之后的每一帧，要么在这个原子快照里，
-- 要么在 viewer 的本地缓冲里；返回的 :seq frontier 用来去掉重叠。
local snapshot = {
  redis.call('GET',   KEYS[1]) or '0',      -- 当前 seq
  redis.call('LRANGE', KEYS[2], 0, -1)      -- 当前 backlog 全部
}
redis.call('PUBLISH', ARGV[1], ARGV[2])     -- 发一个 frontier 标记帧
return snapshot
```

配合订阅侧的 `captureSequenceFrontier` / `deferSequenceDelivery`：开频道后**先不投递**，
等这个脚本返回 frontier，再把缓冲里 `seq > frontier` 的帧放行。

结果：**既不丢也不重**。这是这套架构里最关键的一段代码。

发布侧同理，一次往返完成「分配序号 + 进 backlog + 裁剪 + 续命 + 写标记 + 广播」：

```lua
-- internal/replay.ts: PUBLISH_REPLAY_LUA（节选）
local seq = redis.call('INCR', KEYS[1]) - 1
local payload = ARGV[2] .. seq .. ARGV[3]        -- 序号拼进 payload，不解析 JSON
redis.call('RPUSH', KEYS[2], payload)
local count = redis.call('LLEN', KEYS[2])
while count > maxItems or bytes > maxBytes do    -- 双预算裁剪
  local removed = redis.call('LPOP', KEYS[2]); bytes = bytes - #removed; count = count - 1
end
redis.call('PEXPIRE', KEYS[2], ttlMs)            -- 列表自过期，不用人工清理
redis.call('HSET', KEYS[4], 'id', ..., 'seq', seq, 'done', ...)
redis.call('PUBLISH', ARGV[1], payload)
return seq
```

注意 payload 是「前缀 + 序号 + 后缀」**字符串拼接**，不是 `cjson.encode` ——
注释明说理由是「保留空数组和数字精度」。这是被 cjson 坑过的痕迹。

### 坑 2：pub/sub 会乱序

Redis Cluster 下多个发布者/多分片，同一个频道的消息可能乱序到达。LibreChat 用
**订阅侧 reorder buffer**：

```ts
// RedisEventTransport.ts:100
interface ReorderBuffer {
  nextSeq: number;                    // 下一个期望的序号
  pending: Map<number, PubSubMessage>; // 等着前驱的帧
  flushTimeout: ...;                  // 最多等多久
  deliveryDeferred: boolean;          // 等 frontier 期间先不投递
}
```

```ts
// internal/timing.ts
export const REDIS_EVENT_REORDER_TIMEOUT_MS = 500;  // 超过就强制放行，宁可错序不可卡死
const MAX_BUFFER_SIZE = 100;                        // 缓冲上限，防内存爆
```

**500ms 强制放行**是这套设计的诚实之处：它承认「有序」是尽力而为，不是绝对保证。
代价是极端情况下可能短暂错序；换来的是永不死锁。

### 坑 3：每条 token 一次 Redis 往返

一次回复几千帧，每帧一次 Redis RTT（本机 ~0.2ms，跨机 ~1ms）就是几秒额外延迟。
LibreChat 用**可配合并窗口**：

```ts
// internal/coalescing.ts
const MAX_COALESCE_WINDOW_MS = 1000;      // 窗口上限，再大只是增加 UI 陈旧度
export const MAX_COALESCED_EVENTS = 64;   // 攒满就立刻发，不等窗口
export const MAX_COALESCED_BYTES = 128 * 1024;

export function resolveCoalesceWindowMs(): number {
  const raw = Number(process.env.STREAM_DELTA_COALESCE_MS ?? 25);  // 默认 25ms
  if (!Number.isFinite(raw) || raw <= 0) return 0;                 // 显式 0 = 关闭
  return Math.min(Math.floor(raw), MAX_COALESCE_WINDOW_MS);
}
```

注释点出一个**必须同时改两侧**的约束：

> 订阅侧的 resume frontier 假设「事件在 durable chunk log 里可见的时间，不会显著早于它的序号
> 落到共享计数器上」；只批处理发布侧而不同时批处理 durable 侧，会把同 tick 的偏差放大成
> 整个窗口的竞态。

25ms 窗口对用户是不可感知的（人眼阈值 ~100ms），往返次数降到 1/40~1/64。

### 4.1 补充：SSE 心跳

```ts
// stream/keepalive.ts
export const SSE_KEEPALIVE_FRAME = ':\n\n';
```

只是 SSE 注释行，客户端按规范跳过，中间设备看到字节。Hlaudz 已经这么做了，而且做得更细：

```python
# backend/app/turn_job.py:110
_HEARTBEAT_FRAME = ": keep-alive\n\n"   # 不带 id:，不推进续看游标
```

这一点 Hlaudz 现状不比 LibreChat 差，差别只在于 Hlaudz 的计时器也在进程内。

---

## 5. 「任意实例都能接任意流」的另一半：写侧

只解决「B 能读到 A 的事件」还不够。用户的 `POST /stop`、`POST /queue`（中途插话）也可能
落到非 owner 实例。LibreChat 专门为此开了一个方法：

```ts
// GenerationJobManager.ts:6916
/** 从任意 API 副本发布一个耐久的非流式控制事件。
 *  `emitChunk` 刻意要求本地 runtime，但 steer/arm 路由可能在非 owner 上执行。
 *  owner 在本地时走正常的排序/缓冲路径；否则「受 generation 围栏约束的 chunk append」
 *  必须先落盘，然后才直接 publish。 */
async emitChunkFromAnyReplica(streamId, event, expectedCreatedAt): Promise<boolean> {
  const runtime = this.runtimeState.get(streamId);
  if (runtime != null && runtime.createdAt === expectedCreatedAt && this.isCurrentRuntime(...)) {
    await this.emitChunk(streamId, event, { durable: true, expectedCreatedAt });
    return true;
  }
  const appended = await this.jobStore.appendChunk(streamId, {...}, expectedCreatedAt);
  if (appended === false) return false;         // 围栏拒绝：这个 generation 已经不是我该写的
  const published = await emitChunkWithReceipt(this.eventTransport, streamId, event, expectedCreatedAt);
  return published !== false;
}
```

两个设计要点：

1. **先 durable 后 publish**。非 owner 写事件时，先落持久 chunk log 再广播 —— 这样即使广播
   丢了，重连时也能从 log 重建。注释里 `durable: true` 的说明很精确：「否则跨副本重连会
   重建出没有这些事件的内容」。
2. **`expectedCreatedAt` 是围栏**。不是「随便写一个流」，而是「写**这一个** generation」。
   如果这个流已经被新 generation 替换，`appendChunk` 返回 false，写入被拒。

Hlaudz 在这半边其实**已经做对了方向**：`stopsignal` / `steerbus` 就是「写侧去 Redis」。
缺的是「事件流」这半边，以及 `expectedCreatedAt` 这种围栏（Hlaudz 用 `generation_id` 做键，
效果等价）。

另一个非 owner 场景：

```ts
// GenerationJobManager.ts:3245
// Cross-replica scenario: create (or replace) the minimal runtime state
// from the durable generation currently owning this stream ID.
```

非 owner 订阅时，从 `jobStore.getJob()` 重建一个**最小 runtime**（只够转发事件，不含
provider 句柄/abort controller 之类的本地资源）。所以「runtime」不是「这个进程在跑生成」，
而是「这个进程在替某个流做代理」。这个概念的引入让 `subscribe` 在所有实例上都可用。

---

## 6. 幂等认领 vs Hlaudz 的 409

Hlaudz 用 409 拒绝并发生成：

```python
# backend/app/routers/chat.py:921
acquired = await team_store.acquire_generation(user["id"], session_id, generation_id)
if not acquired:
    ...
    raise HTTPException(status_code=409, detail={       # :954
        "error": "generation_active", "active_generation_id": active, ...})
```

而且判定是**诚实**的 —— 会先问「那个 owner 还活着吗」：

```python
# chat.py:937
if active and not is_generation_live(user["id"], session_id, active):
    if await _gen_owner.is_generation_dead_elsewhere(user["id"], session_id, active):   # :938
        ...converge...   # owner 真死了才接管
    else:
        logger.info("拒绝并发生成（owner 仍存活）...")   # :951  真的有人在跑
```

LibreChat 换了个方向：**不拒绝，认领失败就附加到已有流**。

```ts
// GenerationJobManager.ts:3399
async claimGeneration(userId, clientRequestId, streamId, conversationId) {
  const value: TokenIdempotencyClaim = { streamId, conversationId, claimedAt, claimToken: randomUUID(), ... };
  const legacyResult = await this.jobStore.claimIdempotencyKey(legacyKey, value, LEGACY_TTL);
  if (legacyResult.claimed) { ... return { claimed: true, existing: value }; }
  ...
}
```

```ts
// interfaces/IJobStore.ts:733
/** Value stored under an idempotency claim: the stream a retried request should attach to. */
export interface IdempotencyClaimValue {
  streamId: string;
  conversationId: string;
  claimedAt?: number;     // 让失败的重复请求区分「赢家还在启动」和「早就清掉了」
  claimToken?: string;
  startedAt?: number;     // 一旦存在，job 缺失只可能是「已启动/已清理」，不能再当废弃租约接管
  ...
}
```

语义区别：

| | Hlaudz | LibreChat |
| --- | --- | --- |
| 第二个并发请求 | 409 + 「先点停止」 | 200，但**附加到第一条流**，不新建生成 |
| 判定依据 | DB 租约 + Redis 判活 | Redis 幂等键 SET NX |
| 用户看到 | 一句提示 | 同一条流（无感） |
| 代价 | 需要客户端处理 409 | 需要 clientRequestId（客户端生成并去重） |

**这不是技术优劣，是产品决策。** Hlaudz 的「一次会话一次生成」是显式约束（`chat.py:920` 注释：
「Without this lease two tabs can interleave writes into the same checkpoint, and stop becomes
ambiguous」），而且 409 的文案是给人看的。要改的话是改产品行为，不是改实现。

**但要注意：`claimGeneration` 的 legacy/primary 双键机制是为滚动升级设计的。**
注释写明：

> Old replicas know only this key. Claiming it first means a mixed fleet still has exactly one
> admission winner; the primary is a mirror/fence, never an independent chance to start a billed generation.

Hlaudz 从零做不需要这套 —— 这是**两代 API 并存**的债务，不是设计优点。**不要照抄。**

---

## 7. 要不要抄：分项清单

### 值得借鉴（按性价比排序）

| # | 做法 | 为什么值 | Hlaudz 落地成本 |
| --- | --- | --- | --- |
| 1 | **抽 `TurnJobStore` / `EventTransport` 两个接口**，内存实现先行 | 唯一能「单机不变、集群切换」的路径。不做这层，后面全是改调用点 | 小（纯重构，行为不变，可测） |
| 2 | **SUBSCRIBE→原子快照→frontier 去重叠** | 这是晚到订阅的唯一正解；Hlaudz 现在的 `sync_frames` 已在内存里实现了同样的三态，搬到 Lua 只是换实现 | 中 |
| 3 | **序号由 Redis INCR 分配** | 「谁发谁编号」在有多个发布者时必然冲突 | 小 |
| 4 | **reorder buffer + 超时强制放行** | 保证「不死锁」比「绝对有序」重要 | 小 |
| 5 | **合并窗口（25ms / 64 帧 / 128KB）** | 没有它，token 级外置的延迟代价不可接受 | 小 |
| 6 | **backlog 双预算裁剪 + PEXPIRE 自过期** | 防「一轮几万帧」和「几百条巨型工具结果」；Hlaudz 的内存版已有同样双预算思路 | 小 |
| 7 | **写侧「任意副本」也要支持**（Hlaudz 已有 stopsignal/steerbus，缺事件流） | 只解决读侧的话，停止/插话仍会打到非 owner | 中 |
| 8 | **`demand` 标记：只在有人看时保留重放** | 省 Redis 内存；对长连接场景有意义 | 小 |
| 9 | **hash tag `{streamId}`** | 现在不做也行，但上 Cluster 前必须改，一次到位更省事 | 极小 |

### 不要照抄

| 东西 | 为什么不抄 |
| --- | --- |
| `GenerationJobManager` 9,882 行 + `RedisJobStore` 267 KB / 5,987 行 | 这是两代 API + 滚动升级 + 12 个正交特性的叠加。Hlaudz 从零做 1/10 就够 |
| `legacy` / `primary` 双键幂等 | 纯粹是「新旧副本混跑」的兼容债务 |
| `generationEpoch` / `abortAck` / `providerDrain` | 服务于「replacement generation（替换生成）」这个概念，Hlaudz 没有 |
| `generationProtocolVersion` v1/v2 双写 | 同上 |
| `SteeringLifecycle` / `SteerRecovery` 全套 | Hlaudz 的插话语义简单得多（一个 Redis list + 轮询搬运已经够） |

---

## 8. 如果 Hlaudz 要迁移：一个分阶段路线

**最大的坑必须先说清楚**：Hlaudz 的 `TurnJob.blocks_provider` 是一个**闭包**，
指向生成协程里那个「就地 append 的 blocks 列表」：

```python
# backend/app/turn_job.py:212
self.blocks_provider: Callable[[], list[dict]] = lambda: []
# backend/app/turn_job.py:304 set_blocks_provider
"""**必须传一个会返回最新状态的函数，而不是一份快照**"""
```

这个闭包在别的实例上**不存在**。所以外置不是「把 deque 换成 Redis list」那么简单 ——
`sync` 快照这条路必须改成从 durable 状态（DB 或 Redis 里的 blocks）重建。
LibreChat 的做法是 `jobStore` 里存 `resumeState` + `contentParts` + `runSteps`，
`subscribeWithResume` 从这里读，不读内存闭包。这是迁移里**唯一需要改动数据模型**的地方。

分阶段：

### 阶段 0：现状（可上线，有粘性）

粘性 LB + 进程内 `_JOBS`。约束写在部署文档里：**nginx 不能换成无 hash 的轮询**。

### 阶段 1：抽接口（零行为变化，零风险）

```python
class TurnJobStore(Protocol):     # get_job / get_live_job_for_session / start_job
class EventTransport(Protocol):   # attach / publish / sync_frames / finish
```

只把 `_JOBS` 包一层。全部现有测试必须保持绿（含 `test_multi_instance_coverage.py` 里
那条断言 `turn_job.py` 没有 cluster/redis 引用的 must_not —— 到时候要一起改，因为
**接口层加进来后这条断言就该反过来**）。

这一步的价值：验证接口是否够用。如果发现某个调用点必须伸手进 `TurnJob` 内部，
说明抽象漏了，早发现比晚发现好。

### 阶段 2：Redis 实现 + 双写（仍要求粘性）

`TurnJob` 的 `seq` / `replay` 同时写内存和 Redis。`resume_stream` 优先读内存、
读不到读 Redis。此时**还不能去掉粘性**，但可以验证：
- kill 掉 owner 后，另一个实例能否从 Redis 补上已发的事件
- 序号分配是否单调
- 重放与实时是否有重叠/丢失

### 阶段 3：读侧去粘性

`resume_stream` 不再要求本地 job。非 owner 从 Redis 订阅，用 frontier 去重叠。
`blocks` 从 DB/Redis 重建（阶段 1 的数据模型改动在这里生效）。

此时可以把 `nginx.conf` 的 `hash $cookie_hlz_sticky;` 去掉，实测「同一个 cookie 打散到
3 个实例，每一路都能正常收流」。这一步的验收标准和今天验证粘性时相反 —— 今天要
「29/31 稳定落点」，迁移后要「3 个实例都能接」。

### 阶段 4：去掉本地缓冲

只留 Redis，省内存。这一步纯收益，没有新语义。

### 每个阶段的验收方式

沿用本仓库已有的方法（`backend/tests/manual/multi_instance_e2e.py`）：

- 阶段 2/3 关键用例：**起生成 → 强杀 owner 进程 → 从另一个实例的端口续看**，
  必须拿到完整内容（用只存在于该轮的随机 token 判「是否真到了」，
  不能只看「没报错」）。
- 反向确认：把 Redis 写路径 stub 成 no-op，该用例必须变红。

---

## 9. 成本对比：要不要现在做

| | 现状（粘性 LB） | 外置到 Redis |
| --- | --- | --- |
| 任意实例接任意流 | ❌ | ✅ |
| 实例被 kill（滚动升级/崩溃） | 该实例上的流全断，用户需重新发问 | 另一个实例能续上 |
| 扩容/缩容 | 必须 `docker compose restart nginx` 重算 hash | 随时可扩缩 |
| 单帧延迟 | 0 | +0.2ms（本机）/ +1ms（跨机），可被 25ms 合并窗口摊薄 |
| Redis 压力 | 现在只有控制信号 | 每流 ~40 次/秒写（25ms 窗口） |
| 实现复杂度 | 低 | 中（三个坑都要处理） |
| 故障面 | nginx 配错就静默降级成轮询（今天踩过） | Redis 挂了流全断（但 Hlaudz 的 leader/租约本来就依赖 Redis） |

**建议：不急着做，但阶段 1（抽接口）可以现在做。**

理由：
1. 阶段 1 是纯重构，零行为变化，不会引入新故障面；
2. 它把「Hlaudz 的流式状态到底有哪些」这个问题固化成类型，后面无论走哪条路都用得上；
3. 现在的粘性 LB 方案是**能上线的**，只是运维上有个硬约束（不能换 LB 策略、扩缩容要重启 nginx）——
   这个约束已经写进 `docs/20261119-多实例部署操作手册.md`，风险可控。

真正该现在做的，是把「粘性会静默降级」这件事的**监控**补上：
`hash $cookie_hlz_sticky` 在没有 cookie 时会退化（`nginx.conf:192` 的注释已经写了）。
如果能加一条「同一个 cookie 连续 N 次落到不同上游就告警」，风险基本就关掉了。

---

## 10. 源码坐标速查

### LibreChat（`/d/git_codes/LibreChat`，v0.8.9-rc1）

| 事实 | 坐标 |
| --- | --- |
| 内存版 / Redis 版按 `USE_REDIS_STREAMS` 切换 | `packages/api/src/stream/createStreamServices.ts` |
| 实时流接口定义 | `packages/api/src/stream/interfaces/IJobStore.ts:1724` |
| 幂等认领语义 | `IJobStore.ts:733`（`IdempotencyClaimValue`）、`:762`（`IdempotencyClaimResult`） |
| 频道与 key（含 hash tag） | `implementations/RedisEventTransport.ts:24`、`:32` |
| 发布侧原子 Lua | `internal/replay.ts: PUBLISH_REPLAY_LUA` |
| 订阅侧原子快照 + frontier | `internal/replay.ts: READ_REPLAY_LUA` |
| reorder buffer 结构 | `RedisEventTransport.ts:100` |
| reorder 超时 / abort / handoff | `internal/timing.ts`（500 / 3000 / grace / 30000） |
| 合并窗口与上限 | `internal/coalescing.ts`（默认 25ms、上限 1000ms、64 帧、128KB） |
| reorder 缓冲条数上限 | `RedisEventTransport.ts:232` `MAX_BUFFER_SIZE = 100` |
| SSE 心跳帧 | `stream/keepalive.ts` `SSE_KEEPALIVE_FRAME = ':\n\n'` |
| 非 owner 发事件 | `GenerationJobManager.ts:6916 emitChunkFromAnyReplica` |
| 非 owner 建最小 runtime | `GenerationJobManager.ts:3213 getOrCreateRuntimeState`（`:3245` 注释） |
| 幂等认领实现 | `GenerationJobManager.ts:3399 claimGeneration` |
| 订阅入口 | `:5122 subscribe` / `:6193 subscribeWithResume` |
| SSE 路由消费点 | `api/server/routes/agents/index.js:359`（resume）、`:386`（初次） |
| 零粘性依赖 | `grep -rniE "sticky\|affinity" packages/api/src/stream/` → 0 |
| 无亲缘性是前提 | `packages/api/src/mcp/oauth/tokens.ts:1296` |

### Hlaudz（`/d/git_codes/inner_chatgpt`）

| 事实 | 坐标 |
| --- | --- |
| 进程内 job 表 | `backend/app/turn_job.py:113 _JOBS` |
| 订阅者队列上限 4MB | `turn_job.py:77` |
| 订阅者数上限 8 | `turn_job.py:80` |
| 重放预算 2000 帧 / 2MB | `turn_job.py:88-89` |
| 心跳 15s，注释帧不带 id | `turn_job.py:107`、`:110` |
| `TurnJob` 状态（seq/replay/blocks_provider） | `turn_job.py:189`、`:206`、`:208`、`:212` |
| 发布 + 序号重写 | `turn_job.py:296 publish`、`:216 _rewrite_seq` |
| 三态续看（最新 / 增量 / sync 快照） | `turn_job.py:252 sync_frames` |
| 续看决策函数（三选一） | `backend/app/routers/chat.py:1018 _resume_decision` |
| 续看端点 | `backend/app/routers/chat.py:1070 resume_stream` |
| 决策函数查进程内 job | `chat.py:1053` |
| 并发生成 409 + 诚实判活 | `chat.py:937`（判活）、`:954`（抛 409） |
| 停止信号（已跨实例） | `backend/app/cluster/stopsignal.py` |
| 插话总线（已跨实例，200ms 轮询） | `backend/app/cluster/steerbus.py` |
| 粘性 LB 配置 | `backend/deploy/nginx.conf:91 hash $cookie_hlz_sticky;` |

---

## 11. 一句话总结

**Hlaudz 缺的不是「流式重放机制」，是「这个机制住在哪里」。** `TurnJob` 里已经有 seq 游标、
双预算重放缓冲、三态续看、心跳 —— 和 LibreChat 的思路几乎一样，区别只在于它在进程内存里，
所以必须靠粘性 LB 保证「客户端连回同一个进程」。LibreChat 把这套逻辑搬进 Redis（用原子 Lua
解决「补历史与接实时的重叠」这个唯一真难点），于是粘性成了可选项，而不是正确性前提。
搬迁的代价不在「写 Redis 代码」，在 `blocks_provider` 这个闭包必须改成可跨进程重建的
durable 状态 —— 那是唯一需要动数据模型的地方。
---

## 12. 复核记录（理解一遍 + 证明一遍）

按 AGENTS.md §8.6：这份文档写的是「某文件某行是什么」，而这类结论最容易腐烂 ——
加一行注释，所有行号整体漂移，文档看上去毫无变化。所以写了两个脚本：

```bash
python tools/verify/verify_librechat_hlaudz_doc.py          # 65 条断言
python tools/verify/verify_librechat_hlaudz_doc_reverse.py  # 反向确认 7 条
```

断言口径：**文件名写全路径**、**精确片段抄源码原文**、**期望行号**。三类都成立才算证据。

### 本轮抓出的自己的错

| # | 错在哪 | 怎么抓出来的 |
| --- | --- | --- |
| 1 | `TurnJob` 状态行号写成 `:201 / :208 / :211 / :215` | 断言跑出「行 215 是空行」。四个值各差 2~3 行 —— **是本轮典型的「手工数行」错误**（AGENTS.md §8.6 记录过连续三轮同类失误） |
| 2 | `IdempotencyClaimValue` 写成 `:735`，实际 `:733`；`Result` 写成 `:767`，实际 `:762` | 同上 |
| 3 | `MAX_BUFFER_SIZE` 写成 `:231`，实际 `:232` | 同上 |
| 4 | `replay.ts` 四个 Lua 坐标全错（写 `:12/:18/:19/:42/:45`，实际 `:16/:22/:23/:45/:47`） | 断言全红后逐条 `grep -n` 重取 |
| 5 | `createStreamServices.ts` 的 `StreamServices` 写成 `:44`、`useRedis` 写成 `:70`，实际 `:45`、`:74` | 同上 |
| 6 | `RedisEventTransport.ts` 的 `seq`/`backlog` key 写成 `:37/:38`，实际 `:34/:35` | 同上 |
| 7 | 文件大小写的是「102 / 273 / 13.7 / 76 KB」，实测是「100 / 267 / 13.4 / 74 KB」 | 顺手写的小数，没实测就填了。**这类「看着差不多」的数字最该被断言**，所以加了 `SIZE_CASES` 用行数而非字节数（行数比字节数稳定） |

**共同模式**：所有错都出在「我手数行号」。修法不是更仔细，是**让脚本算** ——
期望值应当先由脚本打印出来，再填进断言表。

### 哪些结论是「补漏」（原文/上一轮没有的）

1. **零粘性依赖是可证伪的**：`packages/api/src/stream/` 与 `docker-compose*.yml` + `nginx*`
   的 `sticky|affinity|ip_hash` 命中数都是 0，而且 `mcp/oauth/tokens.ts:1296` 那段注释
   反过来说明 LibreChat 把「无亲缘性」当设计前提。这三条现在都在 `check_must_not()` 里每次重跑。
2. **「先订阅→原子快照→frontier 去重」是唯一真难点**：发布侧 Lua 只是「分配序号 + 追加 + 裁剪」，
   真正解决「晚到订阅者」的是 `READ_REPLAY_LUA` 返回的 `:seq frontier`。
3. **`blocks_provider` 是闭包**，这是 Hlaudz 迁移里唯一必须改数据模型的地方。
   这条是从 `turn_job.py:304` 的 docstring（「必须传一个会返回最新状态的函数，而不是一份快照」）
   反推出来的 —— 它不是为新架构设计的，但对新架构是硬阻塞。

### 哪些是「订正」

| 上一轮理解 | 订正 |
| --- | --- |
| 「LibreChat 也是靠粘性会话」 | 反了。它是**假设无粘性**，凡需串行处自己加跨副本锁 |
| 「Hlaudz 的 cluster 层已经有跨实例总线，所以多实例做了一半」 | 方向不对。stopsignal / steerbus 是**控制信号**，事件流完全没有跨实例通道 |
| 「LibreChat 的 `claimGeneration` 是更优做法」 | 它是**滚动升级的债务**（legacy/primary 双键），不是设计优点，已在 §7 标为「不要照抄」 |

### 已知局限（写清楚，不要被当成已验证）

- 只读了 `packages/api/src/stream/` 与相关路由。**没有**跑起来实测（不像 Hlaudz 那套
  `multi_instance_e2e.py`）。所以文档里关于 LibreChat 的结论都是**静态代码事实**，
  不是运行时实证。
- 因此 §6 的「LibreChat 是附加到已有流」依据是 `IdempotencyClaimValue` 的注释
  （「the stream a retried request should attach to」）+ `subscribe` 的调用点，
  而非抓到一个真实的重复请求。
- §8 的迁移路线是设计建议，**未实施、未验证**。每个阶段都附了验收方式，但验收还没跑过。
