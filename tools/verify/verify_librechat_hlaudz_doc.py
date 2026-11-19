"""核验《LibreChat 流式架构 vs Hlaudz》文档里的坐标与结论（2026-11-19）。

## 为什么需要它

那份文档通篇是「某文件某行是什么」，而「某行是什么」是最容易腐烂的东西：
加一行注释、改一次 import，所有行号整体漂移，文档却看上去毫无变化。
所以把每条事实钉成断言，谁改了源码就立刻变红。

## 断言三要素（AGENTS.md §8.6）

1. **文件名显式**——两个仓库都有 `index.js` 之类，必须写全相对路径；
2. **精确片段抄源码原文**——不许写「我以为的样子」；
3. **期望行号**——坐标漂移立刻暴露。

## 用法

    python tools/verify/verify_librechat_hlaudz_doc.py

需要两个仓库都在固定位置（脚本顶部常量可改）：

    LIBRECHAT = D:/git_codes/LibreChat
    HLAUDZ    = D:/git_codes/inner_chatgpt
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

LIBRECHAT = Path("D:/git_codes/LibreChat")
HLAUDZ = Path("D:/git_codes/inner_chatgpt")

LC_VER = "v0.8.9-rc1"
LC_GIT = "12b5e9023"

# (说明, 文件, 行号, 必须出现的精确片段)
CASES: list[tuple[str, Path, int, str]] = [
    # ---------------------------------------------------- LibreChat: key 布局
    ("hash tag 频道名", LIBRECHAT / "packages/api/src/stream/implementations/RedisEventTransport.ts",
     24, "const CHANNELS = {"),
    ("hash tag 键族", LIBRECHAT / "packages/api/src/stream/implementations/RedisEventTransport.ts",
     32, "const KEYS = {"),
    ("events 频道带花括号", LIBRECHAT / "packages/api/src/stream/implementations/RedisEventTransport.ts",
     26, "events: (streamId: string) => `stream:{${streamId}}:events`,"),
    ("seq 键", LIBRECHAT / "packages/api/src/stream/implementations/RedisEventTransport.ts",
     34, "sequence: (streamId: string) => `stream:{${streamId}}:seq`,"),
    ("backlog 键", LIBRECHAT / "packages/api/src/stream/implementations/RedisEventTransport.ts",
     35, "backlog: (streamId: string) => `stream:{${streamId}}:activity-backlog`,"),

    # ---------------------------------------------------- LibreChat: 乱序缓冲
    ("reorder buffer 结构", LIBRECHAT / "packages/api/src/stream/implementations/RedisEventTransport.ts",
     103, "interface ReorderBuffer {"),
    ("nextSeq 字段", LIBRECHAT / "packages/api/src/stream/implementations/RedisEventTransport.ts",
     105, "nextSeq: number;"),
    ("pending map", LIBRECHAT / "packages/api/src/stream/implementations/RedisEventTransport.ts",
     107, "pending: Map<number, PubSubMessage>;"),
    ("MAX_BUFFER_SIZE = 100", LIBRECHAT / "packages/api/src/stream/implementations/RedisEventTransport.ts",
     232, "const MAX_BUFFER_SIZE = 100;"),

    # ---------------------------------------------------- LibreChat: 时序常量
    ("reorder 超时 500ms", LIBRECHAT / "packages/api/src/stream/internal/timing.ts",
     2, "export const REDIS_EVENT_REORDER_TIMEOUT_MS = 500;"),
    ("abort ack 3000ms", LIBRECHAT / "packages/api/src/stream/internal/timing.ts",
     6, "export const REDIS_ABORT_ACK_TIMEOUT_MS = 3_000;"),
    ("handoff 30000ms", LIBRECHAT / "packages/api/src/stream/internal/timing.ts",
     16, "export const REDIS_REPLACEMENT_HANDOFF_MAX_WAIT_MS = 30_000;"),

    # ---------------------------------------------------- LibreChat: 合并窗口
    ("合并窗口上限 1000ms", LIBRECHAT / "packages/api/src/stream/internal/coalescing.ts",
     2, "const MAX_COALESCE_WINDOW_MS = 1000;"),
    ("合并帧数 64", LIBRECHAT / "packages/api/src/stream/internal/coalescing.ts",
     4, "export const MAX_COALESCED_EVENTS = 64;"),
    ("合并字节 128KB", LIBRECHAT / "packages/api/src/stream/internal/coalescing.ts",
     7, "export const MAX_COALESCED_BYTES = 128 * 1024;"),
    ("默认窗口 25ms", LIBRECHAT / "packages/api/src/stream/internal/coalescing.ts",
     22, "const raw = Number(process.env.STREAM_DELTA_COALESCE_MS ?? 25);"),

    # ---------------------------------------------------- LibreChat: replay Lua
    ("PUBLISH_REPLAY_LUA 定义", LIBRECHAT / "packages/api/src/stream/internal/replay.ts",
     16, "export const PUBLISH_REPLAY_LUA = `"),
    ("发布侧 INCR 序号", LIBRECHAT / "packages/api/src/stream/internal/replay.ts",
     22, "local seq = redis.call('INCR', KEYS[1]) - 1"),
    ("发布侧字符串拼接 payload", LIBRECHAT / "packages/api/src/stream/internal/replay.ts",
     23, "local payload = ARGV[2] .. string.format('%d', seq) .. ARGV[3]"),
    ("READ_REPLAY_LUA 定义", LIBRECHAT / "packages/api/src/stream/internal/replay.ts",
     45, "export const READ_REPLAY_LUA = `"),
    ("读侧 PUBLISH frontier", LIBRECHAT / "packages/api/src/stream/internal/replay.ts",
     47, "redis.call('PUBLISH', ARGV[1], ARGV[2])"),

    # ---------------------------------------------------- LibreChat: 接口
    ("IEventTransport 定义", LIBRECHAT / "packages/api/src/stream/interfaces/IJobStore.ts",
     1724, "export interface IEventTransport {"),
    ("emitChunk 返回序号或 void", LIBRECHAT / "packages/api/src/stream/interfaces/IJobStore.ts",
     1765, "emitChunk(streamId: string, event: unknown, generationId?: number): void | Promise<void | number>;"),
    ("IdempotencyClaimValue", LIBRECHAT / "packages/api/src/stream/interfaces/IJobStore.ts",
     733, "export interface IdempotencyClaimValue {"),
    ("IdempotencyClaimResult", LIBRECHAT / "packages/api/src/stream/interfaces/IJobStore.ts",
     762, "export interface IdempotencyClaimResult {"),

    # ---------------------------------------------------- LibreChat: 管理器
    ("跨副本最小 runtime", LIBRECHAT / "packages/api/src/stream/GenerationJobManager.ts",
     3245, "// Cross-replica scenario: create (or replace) the minimal runtime state"),
    ("getOrCreateRuntimeState", LIBRECHAT / "packages/api/src/stream/GenerationJobManager.ts",
     3213, "private async getOrCreateRuntimeState("),
    ("claimGeneration", LIBRECHAT / "packages/api/src/stream/GenerationJobManager.ts",
     3399, "async claimGeneration("),
    ("emitChunkFromAnyReplica", LIBRECHAT / "packages/api/src/stream/GenerationJobManager.ts",
     6916, "async emitChunkFromAnyReplica("),
    ("subscribe", LIBRECHAT / "packages/api/src/stream/GenerationJobManager.ts",
     5122, "async subscribe("),
    ("subscribeWithResume", LIBRECHAT / "packages/api/src/stream/GenerationJobManager.ts",
     6193, "async subscribeWithResume("),

    # ---------------------------------------------------- LibreChat: 其它
    ("SSE 心跳帧", LIBRECHAT / "packages/api/src/stream/keepalive.ts",
     9, "export const SSE_KEEPALIVE_FRAME = ':\\n\\n';"),
    ("装配入口 StreamServices", LIBRECHAT / "packages/api/src/stream/createStreamServices.ts",
     45, "export interface StreamServices {"),
    ("按 USE_REDIS_STREAMS 选实现", LIBRECHAT / "packages/api/src/stream/createStreamServices.ts",
     74, "const useRedis = config.useRedis ?? cacheConfig.USE_REDIS_STREAMS;"),
    ("无亲和性是前提（OAuth 串行化）", LIBRECHAT / "packages/api/src/mcp/oauth/tokens.ts",
     1296, "balancer without session affinity, one user's concurrent requests (a second browser"),
    ("SSE 路由 resume 订阅", LIBRECHAT / "api/server/routes/agents/index.js",
     359, "await GenerationJobManager.subscribeWithResume(streamId, writeEvent, onDone, onError, {"),
    ("SSE 路由初次订阅", LIBRECHAT / "api/server/routes/agents/index.js",
     386, "result = await GenerationJobManager.subscribe(streamId, writeEvent, onDone, onError, {"),

    # ---------------------------------------------------- Hlaudz: turn_job
    ("进程内 job 表", HLAUDZ / "backend/app/turn_job.py",
     113, '_JOBS: dict[tuple[str, str, str], "TurnJob"] = {}'),
    ("订阅者队列 4MB", HLAUDZ / "backend/app/turn_job.py",
     77, "_SUBSCRIBER_QUEUE_LIMIT_BYTES = 4 * 1024 * 1024"),
    ("订阅者上限 8", HLAUDZ / "backend/app/turn_job.py",
     80, "MAX_SUBSCRIBERS = 8"),
    ("重放帧数 2000", HLAUDZ / "backend/app/turn_job.py",
     88, 'REPLAY_MAX_FRAMES = int(os.environ.get("HLZ_TURNJOB_REPLAY_FRAMES", "2000"))'),
    ("重放字节 2MB", HLAUDZ / "backend/app/turn_job.py",
     89, 'REPLAY_MAX_BYTES = int(os.environ.get("HLZ_TURNJOB_REPLAY_BYTES", str(2 * 1024 * 1024)))'),
    ("心跳 15s", HLAUDZ / "backend/app/turn_job.py",
     107, 'HEARTBEAT_IDLE_S = float(os.environ.get("HLZ_TURNJOB_HEARTBEAT_IDLE_S", "15"))'),
    ("心跳帧是注释行", HLAUDZ / "backend/app/turn_job.py",
     110, '_HEARTBEAT_FRAME = ": keep-alive\\n\\n"'),
    ("TurnJob 类", HLAUDZ / "backend/app/turn_job.py",
     189, "class TurnJob:"),
    ("subscribers 列表", HLAUDZ / "backend/app/turn_job.py",
     198, "self.subscribers: list[Subscriber] = []"),
    ("seq 游标", HLAUDZ / "backend/app/turn_job.py",
     206, "self.seq = 0"),
    ("replay deque", HLAUDZ / "backend/app/turn_job.py",
     208, "self.replay: deque[tuple[int, str]] = deque()"),
    ("blocks_provider 闭包", HLAUDZ / "backend/app/turn_job.py",
     212, "self.blocks_provider: Callable[[], list[dict]] = lambda: []"),
    ("set_blocks_provider", HLAUDZ / "backend/app/turn_job.py",
     304, "def set_blocks_provider(self, provider: Callable[[], list[dict]]) -> None:"),
    ("_rewrite_seq", HLAUDZ / "backend/app/turn_job.py",
     216, "def _rewrite_seq(self, frame: str, seq: int) -> str:"),
    ("sync_frames", HLAUDZ / "backend/app/turn_job.py",
     252, "def sync_frames(self, after: int) -> list[str]:"),
    ("publish", HLAUDZ / "backend/app/turn_job.py",
     296, "def publish(self, frame: str) -> None:"),
    ("get_live_job_for_session", HLAUDZ / "backend/app/turn_job.py",
     337, "def get_live_job_for_session(user_id: str, session_id: str) -> Optional[TurnJob]:"),

    # ---------------------------------------------------- Hlaudz: chat 路由
    # ⚠️ 2026-11-19：续看的「查 job」挪进了 _resume_decision，端点自身只做分派。
    # 坐标随后移，断言跟着改 —— 但语义没变（端点不抢租约、不建消息）。
    ("续看决策函数", HLAUDZ / "backend/app/routers/chat.py",
     1018, "async def _resume_decision(user_id: str, session_id: str) -> dict:"),
    ("决策函数查进程内 job", HLAUDZ / "backend/app/routers/chat.py",
     1053, "job = turn_job.get_live_job_for_session(user_id, session_id)"),
    ("续看端点", HLAUDZ / "backend/app/routers/chat.py",
     1093, "async def resume_stream(session_id: str, after: int = 0,"),
    ("并发租约", HLAUDZ / "backend/app/routers/chat.py",
     921, 'acquired = await team_store.acquire_generation(user["id"], session_id, generation_id)'),
    ("诚实判活", HLAUDZ / "backend/app/routers/chat.py",
     938, "if await _gen_owner.is_generation_dead_elsewhere("),
    ("抛 409", HLAUDZ / "backend/app/routers/chat.py",
     954, "raise HTTPException(status_code=409, detail={"),

    # ---------------------------------------------------- Hlaudz: nginx 粘性
    ("粘性 hash 配置", HLAUDZ / "backend/deploy/nginx.conf",
     91, "hash $cookie_hlz_sticky;"),
]

# 文档里写死的版本/规模事实
SIZE_CASES = [
    ("GenerationJobManager 行数", LIBRECHAT / "packages/api/src/stream/GenerationJobManager.ts", 9882),
    ("RedisJobStore 行数", LIBRECHAT / "packages/api/src/stream/implementations/RedisJobStore.ts", 5987),
    ("RedisEventTransport 行数", LIBRECHAT / "packages/api/src/stream/implementations/RedisEventTransport.ts", 1987),
    ("InMemoryJobStore 行数", LIBRECHAT / "packages/api/src/stream/implementations/InMemoryJobStore.ts", 2916),
    ("InMemoryEventTransport 行数", LIBRECHAT / "packages/api/src/stream/implementations/InMemoryEventTransport.ts", 403),
]


def check_lines() -> list[str]:
    fails = []
    for why, path, lineno, needle in CASES:
        if not path.exists():
            fails.append(f"[缺文件] {why}: {path}")
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
        if lineno > len(lines):
            fails.append(f"[越界] {why}: {path.name} 只有 {len(lines)} 行，断言要 {lineno}")
            continue
        actual = lines[lineno - 1]
        if needle not in actual:
            fails.append(
                f"[不符] {why}\n    文件 {path}\n    行 {lineno}\n"
                f"    期望含: {needle!r}\n    实际是: {actual.strip()!r}")
    return fails


def check_sizes() -> list[str]:
    fails = []
    for why, path, expected in SIZE_CASES:
        if not path.exists():
            fails.append(f"[缺文件] {why}: {path}")
            continue
        n = len(path.read_text(encoding="utf-8", errors="replace").split("\n")) - 1
        if n != expected:
            fails.append(f"[行数不符] {why}: 期望 {expected}，实际 {n}")
    return fails


def check_must_not() -> list[str]:
    """否定结论（文档断言的事实），每次重跑。"""
    fails = []
    stream_dir = LIBRECHAT / "packages/api/src/stream"

    # 1) 流式层零粘性依赖 —— 这是文档最核心的论据
    hits = []
    for p in stream_dir.rglob("*.ts"):
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"(?i)\b(sticky|affinity)\b", text):
            line = text[:m.start()].count("\n") + 1
            hits.append(f"{p.relative_to(LIBRECHAT)}:{line}")
    if hits:
        fails.append("[must_not] LibreChat stream 层出现了 sticky/affinity：\n  " + "\n  ".join(hits[:10]))

    # 2) docker-compose 与 nginx 里也没有粘性配置
    hits2 = []
    for p in list(LIBRECHAT.glob("docker-compose*.yml")) + list(LIBRECHAT.glob("nginx*")):
        if p.is_file():
            t = p.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"(?i)\b(sticky|affinity|ip_hash)\b", t):
                hits2.append(f"{p.name}:{t[:m.start()].count(chr(10)) + 1}")
    if hits2:
        fails.append("[must_not] LibreChat compose/nginx 出现粘性配置：\n  " + "\n  ".join(hits2))

    # 3) Hlaudz 的 turn_job.py 里没有 cluster / redis —— 证明它确实是进程内
    tj = HLAUDZ / "backend/app/turn_job.py"
    if tj.exists():
        t = strip_comments(tj.read_text(encoding="utf-8"))
        # 去掉文档字符串（三引号块）后再判，避免「注释里提到 LibreChat」误伤
        t = re.sub(r'""".*?"""', "", t, flags=re.S)
        t = re.sub(r"'''.*?'''", "", t, flags=re.S)
        for word in ("cluster", "redis"):
            if re.search(rf"(?i)\b{word}\b", t):
                fails.append(f"[must_not] Hlaudz turn_job.py 里出现了 {word} —— 文档的「进程内」结论失效")
    else:
        fails.append(f"[缺文件] {tj}")

    return fails


def strip_comments(text: str) -> str:
    out = []
    for line in text.split("\n"):
        idx = line.find("#")
        out.append(line if idx < 0 else line[:idx])
    return "\n".join(out)


def check_doc_numbers() -> list[str]:
    """文档里引用的常量值必须与源码一致（防止改了源码忘了改文档）。"""
    fails = []
    doc = HLAUDZ / "docs/20261119-LibreChat流式架构与Hlaudz多实例对比.md"
    if not doc.exists():
        return [f"[缺文件] {doc}"]
    text = doc.read_text(encoding="utf-8")

    # 文档必须写出这几个数，且不能写成别的值
    must = {
        "REDIS_EVENT_REORDER_TIMEOUT_MS = 500": r"REDIS_EVENT_REORDER_TIMEOUT_MS = 500",
        "默认 25ms": r"STREAM_DELTA_COALESCE_MS \?\? 25",
        "64 帧": r"MAX_COALESCED_EVENTS = 64",
        "128KB": r"MAX_COALESCED_BYTES = 128 \* 1024",
        "MAX_BUFFER_SIZE = 100": r"MAX_BUFFER_SIZE = 100",
        "9,882 行": r"9,882 行",
    }
    for why, pat in must.items():
        if not re.search(pat, text):
            fails.append(f"[文档缺内容] {why}（正则 {pat!r}）")

    # 反例：不该出现旧错误值。
    # **必须先在「复核记录」段之前截断**：那一节要引用旧错值来说明「错在哪」，
    # 引用是对的，不能算过期。曾因没截断导致这条断言误报（把订正记录当错误）。
    body = text.split("## 12. 复核记录")[0]
    bad = ["273 KB", "13.7 KB", "76 KB", "102 KB"]
    for b in bad:
        if b in body:
            fails.append(f"[文档含过期数字] {b!r}（已改成实测值）")

    # 复核段必须存在，否则上面那条截断就成了「随便写个空段落蒙过去」
    if "## 12. 复核记录" not in text:
        fails.append("[文档缺内容] 没有「## 12. 复核记录」段 —— §8.6 要求写清订正/补漏/自我纠错")

    return fails


def main() -> int:
    print(f"LibreChat 期望 {LC_VER} / {LC_GIT}")
    if not LIBRECHAT.exists():
        print(f"!! 找不到 {LIBRECHAT}")
        return 2
    if not HLAUDZ.exists():
        print(f"!! 找不到 {HLAUDZ}")
        return 2

    sections = [
        ("行号 + 精确片段", check_lines()),
        ("文件规模", check_sizes()),
        ("否定结论 (must_not)", check_must_not()),
        ("文档自身内容", check_doc_numbers()),
    ]

    total = 0
    bad = 0
    for name, fails in sections:
        total += 1
        if fails:
            bad += 1
            print(f"\n=== FAIL: {name} ===")
            for f in fails:
                print("  " + f)
        else:
            print(f"=== PASS: {name} ===")

    n_cases = len(CASES) + len(SIZE_CASES)
    print(f"\n断言条数约 {n_cases}；分组 {total}，失败 {bad}")
    if bad:
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())