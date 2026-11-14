"""`auto_dream`：跨会话合并记忆碎片（M4.5）。

提取（`memory_extract.py`）负责「把本轮的新事实写进去」；dream 负责**翻旧账**：
去重、合并碎片、删掉被推翻的事实、维护索引。两者职责不同，不能互相替代 ——
提取时的同 slug 比对只能防「同名重复新建」，压不住「三条记忆讲同一件事」。

**dream 不是「空闲时触发」。** CC 里没有空闲检测：它的触发点和提取器完全一样，
都在轮次结束里、紧挨着（`m0848:3859`）。只是内部有三道硬时间闸
（24h / 5 会话 / 10 分钟扫描节流），实际表现成「隔天悄悄跑」。
Hlaudz 用后台巡检任务实现这一点 —— 语义等价，但不需要挂在某个用户的轮次上。

**dream 不做「从失败中学习」。** 那是提取器的 `feedback` 类型。
dream 只整理已有记忆，输入是「记忆目录现状」，不新增「这轮学到了什么」。

三处**有意偏离 CC**（方案 §4.6）：

1. **锁用 DB 行 + 心跳，不用文件。** CC 用 `.consolidate-lock` 文件；Web 服务里
   多 worker / 容器重启后 PID 视图不同，文件锁不好处理。
2. **多记 `last_attempt` / `last_status` / `runs` / `last_error`。** CC 只有
   `lastConsolidatedAt`，连续崩溃五次也看不出来。
3. **Phase 4（索引重建）拆成可独立重跑的幂等步骤。** 用 `index_pending` 标记：
   Phase 3 一成功就推 `last_consolidated` 并置 `index_pending=1`；索引重建成功后清零。
   被 kill 在重建之前时，下一轮巡检只补重建（很便宜），不用重跑 Phase 3（很贵）。
   CC 是 CLI，重启频率低，可以「反正会重跑」；Web 服务部署频繁，这个代价不可接受。

**启动时不补跑 dream**，只收敛状态（清锁）。启动时正要连 MCP、建连接池，
dream 会抢资源拖慢首批用户的首帧。

## 与方案的一处偏差：dream 多一个 `memory_delete` 工具

方案 §5 说对外只承诺 `memory_list` / `memory_read` / `memory_write` 三件，
**这里不改那个契约** —— root agent 与子代理拿到的仍然是三件套。

但 dream 必须能真的删条目，否则「三条记忆讲同一件事」只能合并成一条、
另外两条变成无人认领的残留（或靠「写空内容」这种 hack，而 `write_memory` 的
merge 语义是「留空=沿用」，根本写不空）。所以 dream 的 registry 里额外挂一个
`memory_delete`，**只在 dream 里存在**：

    root registry      memory_list / memory_read / memory_write
    提取子代理          memory_write
    dream              memory_list / memory_read / memory_write / memory_delete

`tests/test_memory_dream.py::test_root_registry_has_no_memory_delete` 钉住这件事。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiosqlite

from . import memory_store as ms
from .tools import Tool, ToolRegistry

logger = logging.getLogger(__name__)

# 锁过期时间。对齐方案：30 分钟。
# 为什么不是 CC 的 1 小时：Hlaudz 一次 dream 的墙钟上限是 300s（见下），
# 30 分钟的锁过期已经远超「正常最坏情况」，再长只会拖慢故障恢复。
LOCK_STALE_MS = 30 * 60 * 1000

# 一次 dream 的墙钟上限。后台任务必须有硬上限。
DEFAULT_DREAM_TIMEOUT_S = 300.0

# 单次 dream 的轮数上限（Orient 翻页几步 + Consolidate 每条两步）
DEFAULT_DREAM_MAX_TURNS = 16

# 一次 dream 最多删几条。防止模型「大扫除」把记忆清空。
DEFAULT_MAX_DELETES = 20

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS memory_dream_state (
    user_id            TEXT PRIMARY KEY,
    last_consolidated  INTEGER NOT NULL DEFAULT 0,
    last_attempt       INTEGER NOT NULL DEFAULT 0,
    last_status        TEXT    NOT NULL DEFAULT 'idle',
    lock_pid           INTEGER NOT NULL DEFAULT 0,
    lock_at            INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT    NOT NULL DEFAULT '',
    runs               INTEGER NOT NULL DEFAULT 0,
    index_pending      INTEGER NOT NULL DEFAULT 0
);
"""

# 后加的列（旧库靠 ALTER TABLE 补齐）。
_LATE_COLUMNS = (
    "last_error TEXT NOT NULL DEFAULT ''",
    "runs INTEGER NOT NULL DEFAULT 0",
    "index_pending INTEGER NOT NULL DEFAULT 0",
)


def _env_int(name: str, default: int, lo: int = 0) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, int(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, lo: float = 1.0) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, float(raw))
    except (TypeError, ValueError):
        return default


# ---------------- 配置 ----------------


def dream_enabled() -> bool:
    """总开关。``HLZ_MEMORY_DISABLED=1`` 作为排障出口一票否决。"""
    return ms.dream_enabled() and not ms.memory_hard_disabled()


def min_hours() -> float:
    """距上次**成功**合并的最小小时数。默认 24（对齐 CC 的 `minHours`）。"""
    return _env_float("HLZ_MEMORY_DREAM_MIN_HOURS", 24.0, lo=0.0)


def min_sessions() -> int:
    """期间需要被触碰的会话数。默认 5（对齐 CC）。"""
    return _env_int("HLZ_MEMORY_DREAM_MIN_SESSIONS", 5, lo=1)


def scan_interval_s() -> float:
    """巡检间隔。默认 600s（对齐 CC 的 `ce = 600000`）。"""
    return _env_float("HLZ_MEMORY_DREAM_SCAN_INTERVAL", 600.0, lo=10.0)


def dream_timeout_s() -> float:
    return _env_float("HLZ_MEMORY_DREAM_TIMEOUT_S", DEFAULT_DREAM_TIMEOUT_S, lo=10.0)


def dream_max_turns() -> int:
    return _env_int("HLZ_MEMORY_DREAM_MAX_TURNS", DEFAULT_DREAM_MAX_TURNS, lo=2)


def max_deletes() -> int:
    return _env_int("HLZ_MEMORY_DREAM_MAX_DELETES", DEFAULT_MAX_DELETES, lo=0)


# ---------------- 状态表 ----------------


def _now_ms() -> int:
    return int(time.time() * 1000)


async def init_tables() -> None:
    async with aiosqlite.connect(ms.DB_PATH) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.executescript(_CREATE_TABLE)
        for col in _LATE_COLUMNS:
            try:
                await conn.execute(f"ALTER TABLE memory_dream_state ADD COLUMN {col}")
            except Exception:  # noqa: BLE001 - 列已存在
                pass
        await conn.commit()


def _blank_state(user_id: str) -> dict[str, Any]:
    return {
        "user_id": user_id, "last_consolidated": 0, "last_attempt": 0,
        "last_status": STATUS_IDLE, "lock_pid": 0, "lock_at": 0,
        "last_error": "", "runs": 0, "index_pending": 0,
    }


async def get_state(user_id: str) -> dict[str, Any]:
    """读一个用户的 dream 状态；没有行时返回默认值（不建行）。"""
    async with aiosqlite.connect(ms.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000;")
        cur = await conn.execute("SELECT * FROM memory_dream_state WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    return dict(row) if row is not None else _blank_state(user_id)


async def _set_state(user_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    async with aiosqlite.connect(ms.DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute(
            "INSERT INTO memory_dream_state(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING",
            (user_id,))
        await conn.execute(
            f"UPDATE memory_dream_state SET {cols} WHERE user_id=?",
            (*fields.values(), user_id))
        await conn.commit()


async def acquire_lock(user_id: str, *, pid: int = 0, now_ms: int = 0) -> bool:
    """原子抢锁：一个 `UPDATE ... WHERE` 同时完成判断与抢占。

    条件：没有锁 / 锁已过期（``lock_at`` 早于 ``LOCK_STALE_MS`` 之前）。

    为什么必须原子：巡检任务和「设置页立即整理」可能同时到。两个独立的
    SELECT-then-UPDATE 之间会有窗口，让两次 dream 并发 —— 而它们都改写
    同一批记忆文件，后写的会覆盖前写的成果。
    """
    now = now_ms or _now_ms()
    pid = pid or os.getpid()
    async with aiosqlite.connect(ms.DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute(
            "INSERT INTO memory_dream_state(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING",
            (user_id,))
        cur = await conn.execute(
            "UPDATE memory_dream_state SET lock_pid=?, lock_at=?, last_status=? "
            "WHERE user_id=? AND (lock_pid=0 OR lock_at=0 OR lock_at<?)",
            (pid, now, STATUS_RUNNING, user_id, now - LOCK_STALE_MS))
        await conn.commit()
        return cur.rowcount > 0


async def release_lock(user_id: str, *, pid: int = 0) -> None:
    """归还锁。只清 ``lock_pid`` / ``lock_at``，不动状态字段
    （状态由调用方在成功/失败时各自设置）。
    """
    pid = pid or os.getpid()
    async with aiosqlite.connect(ms.DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute(
            "UPDATE memory_dream_state SET lock_pid=0, lock_at=0 "
            "WHERE user_id=? AND lock_pid=?", (user_id, pid))
        await conn.commit()


async def recover_dream_state(reason: str = "server restart") -> int:
    """启动时收敛：清掉所有残留锁，把 ``running`` 改成 ``interrupted``。

    **不能只靠锁过期。** 服务刚重启时锁可能还很新（< 30 分钟），但持锁的进程
    已经不存在了。这与本项目已有的 ``team_store.recover_stale_runtime`` 和
    ``subagent_store.mark_stale_running_as_errored`` 是同一类问题、同一套解法。

    返回被收敛的行数（用于启动日志）。
    """
    async with aiosqlite.connect(ms.DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        cur = await conn.execute(
            "UPDATE memory_dream_state SET lock_pid=0, lock_at=0, "
            "last_status=CASE WHEN last_status=? THEN ? ELSE last_status END, "
            "last_error=CASE WHEN last_status=? THEN ? ELSE last_error END "
            "WHERE lock_pid<>0 OR last_status=?",
            (STATUS_RUNNING, STATUS_INTERRUPTED,
             STATUS_RUNNING, reason, STATUS_RUNNING),
        )
        await conn.commit()
        n = cur.rowcount
    if n:
        logger.info("[dream] 启动收敛：%d 个用户的状态被清理（%s）", n, reason)
    return n


async def list_users_needing_dream(*, now_ms: int = 0) -> list[dict[str, Any]]:
    """列出满足**时间闸**的用户（会话数闸留给调用方判）。

    只查「有记忆的用户」—— 没记忆的人无事可整理。
    """
    now = now_ms or _now_ms()
    threshold = int(now - min_hours() * 3600 * 1000)
    async with aiosqlite.connect(ms.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000;")
        # LEFT JOIN：从没有 dream 状态的用户（last_consolidated=0）也应该入选
        cur = await conn.execute(
            "SELECT m.user_id AS user_id, "
            "       COALESCE(s.last_consolidated, 0) AS last_consolidated, "
            "       COALESCE(s.last_status, 'idle') AS last_status "
            "FROM (SELECT DISTINCT user_id FROM memories) m "
            "LEFT JOIN memory_dream_state s ON s.user_id = m.user_id "
            "WHERE COALESCE(s.last_consolidated, 0) <= ? "
            "  AND COALESCE(s.last_status, 'idle') <> ?",
            (threshold, STATUS_RUNNING),
        )
        return [dict(r) for r in await cur.fetchall()]


async def list_users_with_pending_index() -> list[str]:
    """``index_pending=1`` 的用户：Phase 3 成功了但索引还没重建。

    这条路径**不受时间闸约束** —— 索引重建很便宜（不调模型），
    让「上次被 kill 在重建之前」的用户等 24 小时毫无道理。
    """
    async with aiosqlite.connect(ms.DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        cur = await conn.execute(
            "SELECT user_id FROM memory_dream_state WHERE index_pending=1 AND lock_pid=0")
        return [str(r[0]) for r in await cur.fetchall()]


async def sessions_touched_since(user_id: str, since_ms: int) -> list[str]:
    """``since_ms`` 之后被更新过的会话 id（sessions 表，毫秒时间戳）。"""
    async with aiosqlite.connect(ms.DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        cur = await conn.execute(
            "SELECT id FROM sessions WHERE user_id=? AND updated_at>=? ORDER BY updated_at DESC",
            (user_id, int(since_ms)),
        )
        return [str(r[0]) for r in await cur.fetchall()]


# ---------------- dream 提示词 ----------------
#
# ⚠️ 这份提示词**按 Hlaudz 的实际工具面**写，不照抄 CC 的 `Q()`：
#    CC 的 Phase 1 说「列出记忆目录」「读 MEMORY.md」，Phase 4 说「更新 MEMORY.md」，
#    那需要 Read/Glob/Write/Edit 与只读 shell。
#    Hlaudz 的 dream 只有记忆三件套 + delete，**没有文件工具**（见模块 docstring），
#    所以：
#      - 「列出记忆目录」= `memory_list`（分页）；
#      - 「读 MEMORY.md」= 不需要（索引由代码重建，且模型改不了它）；
#      - Phase 4 整段删掉，改成一句「你不用管索引」。
#    直接抄 CC 的提示词会让模型反复尝试不存在的工具 —— 那是纯浪费轮数。

DREAM_PROMPT = """# Dream：记忆合并

你在做一次 dream —— 对**已经存在**的记忆做一遍整理。
目标：把重复的、漂移的、过时的条目合并成更少更准的记忆，让下一次会话能快速进入状态。

你只有四个工具：
- `memory_list`：分页列举（返回 next_cursor / remaining）
- `memory_read`：读一条的正文与 version
- `memory_write`：新建或覆盖一条（更新时带 if_version）
- `memory_delete`：删掉一条

## Phase 1 — Orient（先看清有什么）

1. 调 `memory_list`，有 `next_cursor` 就继续翻，直到 `remaining` 为 0。
2. 只看 `description`，找出这三类问题：
   - **重复**：多条描述在讲同一件事
   - **矛盾**：两条记忆对同一事实说法不一致
   - **相对日期**：「昨天」「上周」「刚才」这类词
3. 对**你怀疑有问题**的条目用 `memory_read` 读正文确认。
   **不要通读全部条目** —— 那会把轮数烧光，而且多数条目本来就该保持原样。

## Phase 2 — Consolidate（合并掉重复）

对每一组重复 / 矛盾：

1. 选一个 slug 作为**保留者**：用最贴切的那个，**不要新造 slug**。
2. `memory_read` 拿到它的 `version`。
3. 把其它条目的有效信息合并进保留者，`memory_write` 时**带上 if_version**。
   - 相对日期一律换成绝对日期或「此前」这种不依赖时间的说法
   - 矛盾的两条：留下与最近事实一致的那条，把错的那条改对
4. 合并**确认成功**（返回 `outcome == "ok"`）之后，才用 `memory_delete`
   删掉被并进去的那些 slug。**先写后删，顺序不能反** ——
   反过来一旦写入失败，内容就真丢了。

## Phase 3 — 收尾

- 没问题的条目**不要动**。不要为了「看起来整理过了」而改动。
- 什么都不用改就直接结束，不要制造工作。
- **索引（MEMORY.md）由代码在最后重建，你不用管它，也不要尝试写它。**

## 硬约束

- 只整理已有记忆，**不新增**「这轮学到了什么」。新事实的写入是提取器的职责。
- 合并后内容必须**变短、变清楚**。变长了说明你在扩写，不是合并。
- 删条目要有明确依据（它已被合并进某条 / 它被证伪）。**拿不准就不删。**
"""


def build_dream_prompt(user_id: str) -> str:
    return f"{DREAM_PROMPT}\n## 本次整理的记忆库\n\n用户 `{user_id}` 的私有记忆库。\n"


# ---------------- 工具集 ----------------


def _build_delete_tool(user_id: str, budget: "_DeleteBudget") -> Tool:
    """dream 专用的 ``memory_delete``（不在 `MEMORY_TOOL_NAMES` 里，见模块 docstring）。"""

    async def memory_delete(args: dict) -> str:
        slug = str(args.get("slug") or args.get("name") or "").strip()
        if not slug:
            return "[记忆错误] slug 不能为空"
        if slug in budget.deleted:
            return json.dumps({"outcome": "noop", "slug": slug,
                               "note": "本次 dream 已经删过这条"}, ensure_ascii=False)
        # **代码级「先写后删」闸**：本次 dream 一条都没写成功就删，几乎必然是
        # 模型跳过了 Phase 2（读到一半就开始清理）。prompt 已经写明顺序，但
        # 「内容真的丢了」是不可逆的，所以这里不能只靠 prompt。
        if budget.writes == 0:
            return json.dumps({
                "outcome": "refused", "slug": slug,
                "note": "本次 dream 还没有任何成功写入。先 memory_write 把内容合并进"
                        "保留的那条（确认 outcome == ok），再删被并掉的那些。",
            }, ensure_ascii=False)
        if len(budget.deleted) >= budget.limit:
            return json.dumps({
                "outcome": "refused", "slug": slug,
                "note": f"本次 dream 的删除配额（{budget.limit} 条）已用完，"
                        f"剩下的留到下次 dream 处理",
            }, ensure_ascii=False)
        ok = await ms.delete_memory(user_id, slug)
        if not ok:
            return json.dumps({"outcome": "not_found", "slug": slug},
                              ensure_ascii=False)
        budget.deleted.append(slug)
        return json.dumps({"outcome": "deleted", "slug": slug}, ensure_ascii=False)

    return Tool(
        "memory_delete",
        "删除一条记忆。**只在本条已被成功合并进另一个 slug 之后使用**，"
        "或用于删掉被明确证伪的条目。删之前先 memory_read 确认内容已经进了别处。"
        "本次 dream 还没有任何成功写入时它会被拒绝。",
        {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "要删除的 slug"},
            },
            "required": ["slug"],
        },
        memory_delete,
    )


@dataclass
class _DeleteBudget:
    limit: int
    deleted: list[str] = field(default_factory=list)
    # 本次 dream 成功写入的次数 —— 「先写后删」闸的唯一依据
    writes: int = 0


def _wrap_write_counter(tools: list[Tool], budget: "_DeleteBudget") -> list[Tool]:
    """给 ``memory_write`` 计数：只有 ``outcome == "ok"`` 才算真写了。"""
    out: list[Tool] = []
    for tool in tools:
        if tool.name != "memory_write":
            out.append(tool)
            continue
        original = tool.call

        async def _call(args: dict, _orig=original) -> str:
            result = await _orig(args)
            try:
                if json.loads(result).get("outcome") == "ok":
                    budget.writes += 1
            except Exception:  # noqa: BLE001 - 解析失败不影响工具本身
                pass
            return result

        out.append(Tool(name=tool.name, description=tool.description,
                        input_schema=tool.input_schema, call=_call,
                        source=tool.source))
    return out


def build_dream_registry(user_id: str, *, delete_limit: int = 0) -> ToolRegistry:
    """dream 的工具集：记忆三件套 + ``memory_delete``，**没有任何执行能力**。

    对齐 CC 的 ``NGr`` 工具闸（比提取子代理还窄）：不允许 shell、不允许文件工具、
    不允许 MCP。Hlaudz 第一版更保守 —— 连 ``file_read`` / ``file_write`` 都不给，
    理由：它们落在 workspace 而不是记忆目录，给了就要另做「只许写记忆目录」的
    路径闸，那是新的一面被注入内容诱导的攻击面。四个记忆工具足够完成合并。
    """
    from .memory_tools import build_memory_tools

    budget = _DeleteBudget(limit=delete_limit if delete_limit else max_deletes())
    tools = _wrap_write_counter(list(build_memory_tools(user_id)), budget)
    tools.append(_build_delete_tool(user_id, budget))
    reg = ToolRegistry(tools)
    # 把配额挂在 registry 上，方便调用方在结束后读「删了哪些」
    reg._dream_delete_budget = budget  # type: ignore[attr-defined]
    return reg


def deleted_slugs(registry) -> list[str]:
    budget = getattr(registry, "_dream_delete_budget", None)
    return list(getattr(budget, "deleted", []) or [])


# ---------------- 执行 ----------------


@dataclass
class DreamOutcome:
    ran: bool = False
    reason: str = ""
    user_id: str = ""
    written: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    index_rebuilt: bool = False
    error: str = ""
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran, "reason": self.reason, "user_id": self.user_id,
            "written": list(self.written), "deleted": list(self.deleted),
            "index_rebuilt": self.index_rebuilt,
            "error": self.error, "elapsed_s": round(self.elapsed_s, 2),
        }


async def run_phase3(client, user_id: str, *, timeout_s: float = 0.0,
                     max_turns: int = 0,
                     registry=None) -> DreamOutcome:
    """Phase 1+2+3：模型读 -> 合并 -> 删。**不重建索引**（那是 Phase 4）。

    单独暴露是为了让「Phase 3 成功但 Phase 4 前被 kill」这个状态可测（M-23）。
    """
    from .agent_loop import AgentLoop

    started = time.monotonic()
    out = DreamOutcome(ran=True, user_id=user_id)
    reg = registry if registry is not None else build_dream_registry(user_id)

    loop = AgentLoop(
        client, reg, "你是记忆整理代理。严格按用户消息里的阶段执行，不要寒暄。",
        max_turns=max_turns or dream_max_turns(),
        history_store=None,   # dream 的对话不进任何历史
        user_id=user_id,
    )
    history: list[dict] = [{"role": "user", "content": build_dream_prompt(user_id)}]

    written: set[str] = set()

    async def _drive() -> None:
        async for ev in loop.run_stream(history):
            if ev.get("type") == "error":
                out.error = str(ev.get("error") or "")
            elif ev.get("type") == "tool_start":
                name = str(ev.get("name") or "")
                if name == "memory_write":
                    inp = ev.get("input") or {}
                    slug = str(inp.get("slug") or "")
                    if slug:
                        written.add(slug)

    budget = timeout_s or dream_timeout_s()
    try:
        await asyncio.wait_for(_drive(), timeout=budget)
    except asyncio.TimeoutError:
        out.error = f"超过 {budget:.0f}s 未完成"
        logger.warning("[dream] 用户 %s 的 dream 超时（%.0fs）", user_id, budget)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - dream 失败不能影响任何人
        out.error = f"{type(exc).__name__}: {exc}"
        logger.warning("[dream] 用户 %s 的 dream 失败", user_id, exc_info=True)

    out.written = sorted(written)
    out.deleted = deleted_slugs(reg)
    out.elapsed_s = time.monotonic() - started
    return out


async def run_phase4(user_id: str) -> bool:
    """Phase 4：重建 ``MEMORY.md``。幂等、便宜、不调模型，可随时重跑。"""
    try:
        await ms.rebuild_index(user_id)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("[dream] 索引重建失败（用户 %s）", user_id, exc_info=True)
        return False


async def run_dream(client, user_id: str, *, timeout_s: float = 0.0,
                    max_turns: int = 0, registry=None) -> DreamOutcome:
    """跑一次完整 dream：Phase 3 + Phase 4（给测试与手动触发用）。

    Phase 4 **即使 Phase 3 出错也做** —— Phase 3 可能已经改过文件，
    「索引与文件不一致」比「索引没更新」更糟。
    """
    out = await run_phase3(client, user_id, timeout_s=timeout_s,
                           max_turns=max_turns, registry=registry)
    out.index_rebuilt = await run_phase4(user_id)
    return out


async def dream_once(user_id: str, client, *, force: bool = False,
                     now_ms: int = 0) -> DreamOutcome:
    """对一个用户做一次完整判定 + 执行（含锁、时间闸、会话数闸）。

    ``force=True`` 跳过时间闸与会话数闸（设置页「立即整理」用），
    但仍然尊重锁与开关 —— 否则用户连点两次会并发两个 dream。
    """
    if not dream_enabled():
        return DreamOutcome(
            ran=False,
            reason="dream 已关闭（HLZ_MEMORY_DREAM_ENABLED=0 或 HLZ_MEMORY_DISABLED=1）")

    now = now_ms or _now_ms()
    state = await get_state(user_id)
    if state.get("last_status") == STATUS_RUNNING and \
            int(state.get("lock_at") or 0) > now - LOCK_STALE_MS:
        return DreamOutcome(ran=False, reason="已有 dream 在跑", user_id=user_id)

    since = int(state.get("last_consolidated") or 0)
    if not force:
        if now - since < min_hours() * 3600 * 1000:
            return DreamOutcome(
                ran=False, user_id=user_id,
                reason=f"距上次成功合并不足 {min_hours():.0f} 小时")
        touched = await sessions_touched_since(user_id, since)
        if len(touched) < min_sessions():
            return DreamOutcome(
                ran=False, user_id=user_id,
                reason=f"期间被触碰的会话数不足（{len(touched)} < {min_sessions()}）")

    if not await acquire_lock(user_id, now_ms=now):
        return DreamOutcome(ran=False, reason="没抢到 dream 锁（别的任务在跑）",
                            user_id=user_id)

    try:
        await _set_state(user_id, last_attempt=now, last_status=STATUS_RUNNING,
                         last_error="", runs=int(state.get("runs") or 0) + 1)
        out = await run_phase3(client, user_id)

        if out.error:
            # last_consolidated **保持不变** —— 失败不算「合并过了」，下次条件
            # 满足要重新跑（M-21）。
            await _set_state(user_id, last_status=STATUS_FAILED,
                             last_error=out.error,
                             # 改过文件就必须重建索引，哪怕本轮报错
                             index_pending=1 if (out.written or out.deleted) else 0)
            return out

        # Phase 3 成功：**先推 last_consolidated，再重建索引**（方案 §4.6 ③）。
        # 被 kill 在重建之前时，`index_pending=1` 会让下一轮只补重建。
        await _set_state(user_id, last_status=STATUS_IDLE,
                         last_consolidated=_now_ms(), last_error="",
                         index_pending=1)
        out.index_rebuilt = await run_phase4(user_id)
        if out.index_rebuilt:
            await _set_state(user_id, index_pending=0)
        return out
    except asyncio.CancelledError:
        # 被取消（服务关闭 / 手动停止）：状态停在 running 会让下一次误判，
        # 明确记成 interrupted 后重抛。
        await _set_state(user_id, last_status=STATUS_INTERRUPTED,
                         last_error="cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 - 任何意外都要留下可诊断的痕迹
        logger.warning("[dream] 用户 %s 的 dream 异常", user_id, exc_info=True)
        await _set_state(user_id, last_status=STATUS_INTERRUPTED,
                         last_error=f"{type(exc).__name__}: {exc}")
        return DreamOutcome(ran=True, user_id=user_id,
                            error=f"{type(exc).__name__}: {exc}")
    finally:
        await release_lock(user_id)


async def sweep_pending_index(user_ids: list[str]) -> int:
    """给 ``index_pending=1`` 的用户补重建索引。返回成功个数。"""
    done = 0
    for user_id in user_ids:
        if await run_phase4(user_id):
            await _set_state(user_id, index_pending=0)
            done += 1
    return done


# ---------------- 后台巡检 ----------------


@dataclass
class DreamLoopHandle:
    task: Optional[asyncio.Task] = None
    stopped: bool = False

    async def stop(self) -> None:
        self.stopped = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _take_client(client_provider, user_id: str):
    """取一个可用的 LLM client；取不到返回 None。

    例外都吞掉：一个用户拿不到 client 不该阻断其他用户。
    """
    if client_provider is None:
        return None
    try:
        return await client_provider(user_id)
    except Exception:  # noqa: BLE001
        logger.debug("[dream] 用户 %s 没有可用 client", user_id, exc_info=True)
        return None


async def _close_client(client) -> None:
    closer = getattr(client, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:  # noqa: BLE001
        pass


async def _tick(client_provider=None, *, now_ms: int = 0) -> list[DreamOutcome]:
    """一次巡检：先补索引 -> 再逐用户判闸跑 dream。

    ``client_provider(user_id)`` 返回该用户可用的 LLMClient。
    Hlaudz 的模型配置是**每用户**的，所以没有全局 client。
    """
    if not dream_enabled():
        return []
    outcomes: list[DreamOutcome] = []

    # 1) 补上次没做完的 Phase 4（便宜，不调模型，不受时间闸约束）
    try:
        pending = await list_users_with_pending_index()
        if pending:
            await sweep_pending_index(pending)
    except Exception:  # noqa: BLE001
        logger.warning("[dream] 补重建索引失败", exc_info=True)

    # 2) 正常路径
    try:
        candidates = await list_users_needing_dream(now_ms=now_ms)
    except Exception:  # noqa: BLE001
        logger.warning("[dream] 列举候选用户失败", exc_info=True)
        return outcomes

    for row in candidates:
        user_id = str(row.get("user_id") or "")
        if not user_id:
            continue
        client = await _take_client(client_provider, user_id)
        if client is None:
            outcomes.append(DreamOutcome(ran=False, user_id=user_id,
                                         reason="没有可用的 LLM client"))
            continue
        try:
            outcomes.append(await dream_once(user_id, client, now_ms=now_ms))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 一个用户失败不影响其他用户
            logger.warning("[dream] 用户 %s 巡检异常", user_id, exc_info=True)
        finally:
            await _close_client(client)
    return outcomes


async def dream_loop(client_provider=None, *, interval_s: float = 0.0) -> None:
    """后台巡检循环。挂在 ``lifespan`` 里。

    先睡再跑：启动时正要连 MCP、建连接池，dream 会抢资源、拖慢首批用户首帧。
    方案 §4.6 明确要求「启动时不补跑」，只收敛状态。
    """
    period = interval_s or scan_interval_s()
    while True:
        try:
            await asyncio.sleep(period)
            await _tick(client_provider)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 巡检永远不能把进程带崩
            logger.warning("[dream] 巡检失败（忽略）", exc_info=True)


def start_dream_loop(client_provider=None) -> DreamLoopHandle:
    handle = DreamLoopHandle()
    handle.task = asyncio.create_task(dream_loop(client_provider))
    return handle


async def default_client_provider(user_id: str):
    """给 dream 用的 LLM client（**每用户**，不带 MCP / skill 装配）。

    不能用 ``RuntimeHub.get_user_runtime``：那会连带连 MCP、拼系统提示词、
    占配额与缓存槽位 —— 后台整理记忆不该付这份代价。
    ``build_user_model`` 只解析配置 + 造一个 httpx client，正是需要的量级。
    """
    from .agent import build_user_model

    model = await build_user_model(user_id, None,
                                   system_prompt="你是记忆整理代理。")
    return getattr(model, "client", None)
