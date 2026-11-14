"""M3.5：LLM 相关性选择器 + 并发预取（**可选增强，默认关闭**）。

> 依据：`docs/20261112-Hlaudz记忆系统实施方案.md` §3.7
> 上游核实：`docs/20261114-CC319记忆触发时机核实.md` §2.4 / §5.1

## 它解决什么问题

M3 的关键词排序有个根本盲点：**它不知道用户在问什么**。用户问
「上次那个报错怎么解决的」，一条标题为「构建失败处理」的记忆应该命中，
但两者没有共同词 —— `_hit_score` 给 0 分，那条记忆永远进不来。

选择器把「记忆清单 + 本轮查询」送一次小模型，让它直接判断相关性。

## 为什么默认关闭

方案 §3.7 与核实文档都写明：**CC 的默认文件记忆形态根本不跑选择器**
（`lSe()` 门控，`tengu_moth_copse` 默认 false）。选择器是「工具存储 / MCP
记忆库」形态的配套，与 `AutoMem` 互斥。Hlaudz 的默认形态对齐 CC 的默认形态。

代价也很实在：**每次模型调用多一次小模型调用**。一个 10 步任务 = 10 次额外调用。
所以：

- `HLZ_MEMORY_SELECTOR=1` 才启用；
- **必须**指定 `HLZ_MEMORY_SELECTOR_MODEL`（不允许回落主模型 —— 那会让成本翻倍）；
- **每 generation 的选择器调用次数有硬上限**（`HLZ_MEMORY_SELECTOR_MAX_CALLS`，默认 3）。

## 为什么不放进 `message_injector`

现有注入器是「每轮开头同步返回」的形态。而预取的整个意义是
**「这一刻可能还没有值」**。同一件事用注入器表达就要 `await`，那正好拖慢首帧。

CC 的做法（`m0848:7294` / `:10602`）是把预取拆成两个挂点：

    模型调用前  -> 只启动，不等待（返回句柄）
    工具步骤后  -> 若已 settle 且未消费过，才注入

这样首 token 完全不被选择器挡住：选择器慢就等下一轮，快就本轮注入。
Hlaudz 在 `AgentLoop` 的轮循环里同样挂两个点。

## 去重：slug 集合，不是布尔标志

预取是**每次模型调用一次**（`Wc` 每次重新进入），不是每次 generation 一次。
一次用户轮次触发 10 次模型调用，预取就启动 10 次。若不按 slug 去重，
同一条记忆会被 append 10 遍，模型会把它当强调指令、行为变僵。

CC 的键是 `readFileState`（已进过上下文的文件路径集合）。Hlaudz 用
`_injected_slugs` —— 与 `MemoryInjector` 同一手法，理由也一样：
布尔标志会让「第三轮后台新写的 B」永远注入不进去。

## 会话级字节预算

`j0n.MAX_SESSION_BYTES = 61440`：整个会话累计注入过的相关记忆内容超限后
**不再召回**（不是召回后丢弃）。防「越聊越多、越聊越慢」。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional

from . import memory_store as ms

logger = logging.getLogger(__name__)

# 选择器输出上限。对齐 CC 的 `.slice(0, 5)`（`m0591:153870`）。
DEFAULT_SELECTOR_MAX = 5
# 每个 generation 最多跑几次选择器（成本闸）。CC 没有这道，但它按次计费更狠。
DEFAULT_MAX_CALLS_PER_GENERATION = 3
# 整会话累计召回字节上限（对齐 CC 的 MAX_SESSION_BYTES）。
DEFAULT_SESSION_MAX_BYTES = 61440
# 选择器单次墙钟上限。它必须短 —— 长了就永远赶不上首帧，等于白跑。
DEFAULT_SELECTOR_TIMEOUT_S = 8.0

# 这些 querySource **不触发**选择器（防自激）。
# 对齐 CC 的 `Gzr = {extract_memories, auto_dream, memdir_relevance,
# prompt_suggestion, compact}`。选择器自己若触发选择器，会无限递归。
NO_SELF_TRIGGER = frozenset({
    "extract_memories",   # 后台提取子代理
    "auto_dream",         # dream
    "memory_selector",    # 选择器自己
    "compaction",         # 压缩（它有自己的预算是另一回事）
})

# 首句免责。CC 的 `Zoo.relevant_memories`（`m0591:229587`）第一句是硬约束：
# 「Retrieved for possible relevance — use only if it actually applies」。
# 没有这句，模型倾向于把召回结果当既定事实全盘套用。
SELECTED_PREFIX = (
    "[记忆·相关] 以下内容仅为**可能**相关，确实适用于用户当前问题才使用；"
    "不适用就忽略，不要硬套。"
)

SELECTOR_PROMPT = """你在为下一轮对话挑选相关记忆。最多选 5 条。

规则：
- 只选真正与用户本次提问相关的。**不要因为字面撞词就选** ——
  例如用户问「Python 版本」，一条提到「Python 脚本路径」的记忆并不相关。
- 已经在上文中出现过的不要重复选。
- 拿不准就不选。漏选一条的代价，远小于塞入无关记忆打断思路。

只输出 JSON：{"selected": ["slug1", "slug2"]}"""


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, lo: int = 0) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, int(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, lo: float = 0.1) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, float(raw))
    except (TypeError, ValueError):
        return default


# ---------------- 配置 ----------------


def selector_enabled() -> bool:
    """选择器总开关。默认 **0**（对齐 CC 的默认形态）。

    与 `memory_enabled()` 是两件事：记忆本身开着、选择器可以关。
    """
    return _env_flag("HLZ_MEMORY_SELECTOR", False)


def selector_model() -> str:
    """选择器用的模型 id。**空串 = 未配置 = 整个机制不启用**。

    为什么不回落主模型：一个 10 步任务会让主模型多跑 10 次，成本直接翻倍。
    方案 §3.7 明确要求「必须指定，不允许留空用主模型」。
    """
    return (os.environ.get("HLZ_MEMORY_SELECTOR_MODEL") or "").strip()


def selector_max() -> int:
    return _env_int("HLZ_MEMORY_SELECTOR_MAX", DEFAULT_SELECTOR_MAX, lo=1)


def max_calls_per_generation() -> int:
    return _env_int("HLZ_MEMORY_SELECTOR_MAX_CALLS",
                    DEFAULT_MAX_CALLS_PER_GENERATION, lo=0)


def session_max_bytes() -> int:
    return _env_int("HLZ_MEMORY_SESSION_MAX_BYTES", DEFAULT_SESSION_MAX_BYTES, lo=0)


def selector_timeout_s() -> float:
    return _env_float("HLZ_MEMORY_SELECTOR_TIMEOUT_S", DEFAULT_SELECTOR_TIMEOUT_S)


def prefetch_enabled() -> bool:
    """完整启用条件：记忆开着 + 选择器开着 + 模型已指定。"""
    return (ms.memory_enabled() and not ms.memory_hard_disabled()
            and selector_enabled() and bool(selector_model()))


# ---------------- 检索线索 ----------------


def last_user_text(history: list[dict]) -> str:
    """取最后一条**非注入**的 user 消息。

    对齐 CC 的 `findLast(m => m.type === "user" && !m.isMeta)`：
    注入块本身就是 user role（attachment 通道），不能拿它当查询线索 ——
    否则第二轮会「用上一轮注入的记忆去检索记忆」，相关度越跑越偏。
    """
    from .memory_inject import VOLATILE_KEY

    for msg in reversed(history or []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        if msg.get(VOLATILE_KEY):
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def query_is_usable(query: str) -> bool:
    """纯单字 / 空串不值得花一次模型调用。对齐 CC 的 `!/\\s/.test(N.trim())`。

    CC 的判据是「必须含空白」—— 即至少两个词。中文没有空格，所以这里放宽成
    「至少 2 个字符」，否则一整句中文会被判成不可用（那是错的）。
    """
    text = (query or "").strip()
    if len(text) < 2:
        return False
    return bool(re.search(r"[\w一-鿿]", text))


# ---------------- 选择器调用 ----------------


def build_selector_input(specs: list[ms.MemorySpec], query: str) -> str:
    """清单 + 查询。清单格式对齐 CC 的 `T7r()`：`- [type] slug (日期): description`。

    `description` 是选择器唯一的判断依据，所以它在记忆写入时就被要求
    「具体到一看就知道讲什么」。
    """
    lines = []
    for m in specs:
        day = ms._short_day(m.updated_at)
        stamp = f" ({day})" if day else ""
        lines.append(f"- [{m.kind}] {m.slug}{stamp}: {m.description}")
    listing = "\n".join(lines) if lines else "（记忆库为空）"
    return f"## 记忆清单（{len(specs)} 条）\n\n{listing}\n\n## 本轮用户问题\n\n{query}\n"


def parse_selector_output(text: str, allowed: list[str]) -> list[str]:
    """从模型输出里取 slug 列表。**只返回 allowed 里存在的**。

    为什么必须过滤：模型会编造 slug，或把清单里的 `[type]` 当成名字。
    直接照单全收会让「删了记忆之后它还在被注入」这类幽灵引用发生在读侧。

    也兼容模型把 JSON 包在 markdown fence 里（实测常见）。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    # 剥 ``` fence
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    # 取第一个 {...}
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        raw = raw[start:end + 1]
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return []
    picked = payload.get("selected") if isinstance(payload, dict) else None
    if not isinstance(picked, list):
        return []
    allowed_set = set(allowed)
    out: list[str] = []
    for item in picked:
        slug = str(item).strip()
        if slug in allowed_set and slug not in out:
            out.append(slug)
    return out[:selector_max()]


async def select_slugs(client, specs: list[ms.MemorySpec], query: str, *,
                       timeout_s: float = 0.0) -> list[str]:
    """调小模型选 slug。任何失败都返回空（退化成关键词排序）。"""
    if not specs:
        return []
    prompt = build_selector_input(specs, query)
    budget = timeout_s or selector_timeout_s()

    async def _call() -> list[str]:
        from .llm_client import ChatResult  # noqa: F401 - 明确依赖面

        res = await client.run(
            [{"role": "system", "content": SELECTOR_PROMPT},
             {"role": "user", "content": prompt}],
            tools=None,
        )
        text = getattr(res, "content", "") or ""
        return parse_selector_output(text, [m.slug for m in specs])

    try:
        return await asyncio.wait_for(_call(), timeout=budget)
    except asyncio.TimeoutError:
        logger.info("[memory] 选择器超时（%.1fs），本轮退化为关键词排序", budget)
        return []
    except Exception:  # noqa: BLE001 - 选择器失败不能影响任何东西
        logger.warning("[memory] 选择器失败，本轮退化为关键词排序", exc_info=True)
        return []


# ---------------- 预算记账 ----------------


class SessionBudget:
    """整会话累计召回字节数的记账器。

    为什么需要：记忆是「越聊越多」的。一个长会话里每轮都注入几条，
    几十轮之后上下文会被记忆撑爆，而且都是陈旧的。CC 的上限是 61440 字节，
    超了就不再召回。
    """

    def __init__(self, limit: int = 0) -> None:
        self.limit = limit if limit > 0 else session_max_bytes()
        self.used = 0

    def exhausted(self) -> bool:
        return self.limit > 0 and self.used >= self.limit

    def charge(self, text: str) -> None:
        self.used += len((text or "").encode("utf-8"))


# ---------------- 预取句柄 ----------------


class PrefetchHandle:
    """预取句柄：只启动，不等待；消费时若未完成则返回空。

    三个字段一一对应 CC 的 `Ivi` 返回对象：
      promise               -> 选择器的 future
      settled_at            -> 完成时刻（None = 还没算完）
      consumed_on_iteration -> 在第几轮被消费（-1 = 还没）

    ⚠️ 不要在 `__init__` 之后 `await` 它 —— 那就退化成同步选择器，
    首帧会被拖慢（实测选择器 300-2000ms）。
    """

    __slots__ = ("promise", "settled_at", "consumed_on_iteration", "_started")

    def __init__(self, coro) -> None:
        self.promise = asyncio.ensure_future(coro)
        self.settled_at: Optional[float] = None
        self.consumed_on_iteration: int = -1
        self._started = time.monotonic()
        self.promise.add_done_callback(self._on_done)

    def _on_done(self, _task) -> None:
        self.settled_at = time.monotonic()

    @property
    def settled(self) -> bool:
        return self.settled_at is not None

    async def take(self, turn: int) -> list[dict[str, Any]]:
        """消费：**已 settle 且未消费过**才返回内容，否则空。

        这就是「不拖慢首 token」的全部秘密：首帧回来时选择器若还没算完，
        这一轮就不注入，留到下一轮再看。
        """
        if self.settled_at is None or self.consumed_on_iteration != -1:
            return []
        self.consumed_on_iteration = turn
        try:
            return await self.promise
        except Exception:  # noqa: BLE001
            logger.warning("[memory] 预取消费失败，本轮不注入", exc_info=True)
            return []

    def cancel(self) -> None:
        if not self.promise.done():
            self.promise.cancel()

    def hidden_by_first_iteration(self) -> bool:
        """CC 的同名遥测（`m0591:153967`）：settle 了但首轮没赶上 = 白做了。

        它直接度量预取预算是否合理。不记这个数，就不知道选择器该给多少超时。
        """
        return self.settled_at is not None and self.consumed_on_iteration == 0


# ---------------- 组合：选择 -> 读正文 -> 渲染 ----------------


def _render_selected(specs: list[ms.MemorySpec], withheld: int = 0) -> str:
    """渲染选择器选中的记忆。与 `format_memory_block` 的差别只有首句。"""
    if not specs:
        return ""
    lines = [SELECTED_PREFIX]
    for m in specs:
        day = ms._short_day(m.updated_at)
        suffix = f"（{m.kind}" + (f"，{day}" if day else "") + "）"
        lines.append(f"- {m.label}：{m.description}{suffix}")
    if withheld > 0:
        lines.append(f"（另有 {withheld} 条记忆与本轮无关或超预算，未注入）")
    return "\n".join(lines)


async def _select_and_render(client, user_id: str, query: str, *,
                             injected: set[str],
                             budget: SessionBudget,
                             scope: str = ms.SCOPE_PRIVATE) -> list[dict[str, Any]]:
    """选择器路径的完整一步：取候选 -> LLM 选 -> 去重 -> 渲染。"""
    from .memory_inject import VOLATILE_KEY

    if budget.exhausted():
        logger.info("[memory] 会话累计召回已达 %d 字节上限，不再召回", budget.limit)
        return []
    try:
        candidates, withheld = await ms.select_with_stats(
            user_id, query_hint=query, scope=scope,
            # 选择器要看全量清单才能挑，所以把预算放宽到「全部候选」；
            # 真正的截断发生在选定之后（最多 5 条）。
            char_budget=10 ** 7, max_entries=10 ** 4,
        )
    except Exception:  # noqa: BLE001
        logger.warning("[memory] 选择器取候选失败", exc_info=True)
        return []
    if not candidates:
        return []

    picked_slugs = await select_slugs(client, candidates, query)
    if not picked_slugs:
        return []

    by_slug = {m.slug: m for m in candidates}
    fresh = [by_slug[s] for s in picked_slugs
             if s in by_slug and s not in injected]
    if not fresh:
        return []
    text = _render_selected(fresh, withheld=withheld)
    if not text:
        return []
    injected.update(m.slug for m in fresh)
    budget.charge(text)
    try:
        await ms.touch_hits(user_id, [m.slug for m in fresh], scope=scope)
    except Exception:  # noqa: BLE001
        pass
    return [{"role": "user", "content": text, VOLATILE_KEY: True}]


# ---------------- 入口：`start_prefetch` ----------------


def start_prefetch(loop, history: list[dict], *, query_source: str = "",
                   client=None, user_id: str = "",
                   state: "PrefetchState | None" = None) -> Optional[PrefetchHandle]:
    """入口闸全部通过才返回句柄，否则 None（对齐 CC 的 `Ivi` 早退）。

    闸的顺序照着 CC：越便宜的越靠前，避免为了判后面而先花代价。
    """
    if not prefetch_enabled():
        return None
    if query_source in NO_SELF_TRIGGER:      # 防自激
        return None
    st = state if state is not None else getattr(loop, "_memory_prefetch_state", None)
    if st is not None and st.budget.exhausted():
        return None
    if st is not None and st.calls >= max_calls_per_generation():
        # 成本闸：一个 10 步任务不该跑 10 次选择器。
        logger.debug("[memory] 本代选择器调用已达上限 %d，跳过", st.calls)
        return None
    query = last_user_text(history)
    if not query_is_usable(query):
        return None

    llm = client or getattr(loop, "_memory_selector_client", None)
    uid = user_id or str(getattr(loop, "user_id", "") or "")
    if llm is None or not uid:
        return None
    scope = getattr(st, "scope", ms.SCOPE_PRIVATE) if st is not None else ms.SCOPE_PRIVATE
    injected = st.injected if st is not None else set()
    budget = st.budget if st is not None else SessionBudget()
    if st is not None:
        st.calls += 1
    coro = _select_and_render(llm, uid, query, injected=injected,
                              budget=budget, scope=scope)
    return PrefetchHandle(coro)


class PrefetchState:
    """一个 generation 内的预取状态（slug 去重集 + 调用计数 + 会话预算）。

    ``injected`` **默认与关键词注入器共用同一个 set**：两条路径各自记一份
    会让同一条记忆既以 ``[记忆]`` 又以 ``[记忆·相关]`` 各注入一次
    （2026-11-14 真机实测：injected_count=2，内容完全相同）。
    共享由 ``install_memory_injector`` 写进 ``loop._memory_injected_slugs``，
    这里通过 ``shared_slugs`` 接过来。
    """

    __slots__ = ("injected", "calls", "budget", "scope")

    def __init__(self, *, scope: str = ms.SCOPE_PRIVATE,
                 budget: Optional[SessionBudget] = None,
                 shared_slugs: Optional[set[str]] = None) -> None:
        self.injected: set[str] = shared_slugs if shared_slugs is not None else set()
        self.calls = 0
        self.budget = budget if budget is not None else SessionBudget()
        self.scope = scope


def _make_prefetch_factory(client, user_id: str, scope: str = ms.SCOPE_PRIVATE):
    """构造传给 `AgentLoop(prefetch_factory=...)` 的回调。

    每次 `run_stream` 开头调一次；它负责**重置本代状态**并返回句柄或 None。
    """

    def _factory(loop, history: list[dict]):
        # 每代重置：`loop` 是跨 generation 复用的，沿用上一代的
        # `injected` 集合会让这一代的记忆永远注入不进来。
        # 但**本代内**要与关键词注入器共用同一个 set（见 PrefetchState 说明）。
        shared = getattr(loop, "_memory_injected_slugs", None)
        loop._memory_prefetch_state = PrefetchState(scope=scope,
                                                    shared_slugs=shared)
        loop._memory_selector_client = client
        return start_prefetch(loop, history, query_source="", client=client,
                              user_id=user_id,
                              state=loop._memory_prefetch_state)

    return _factory


def install_prefetch(loop, client, user_id: str, *,
                     scope: str = ms.SCOPE_PRIVATE) -> bool:
    """给 `loop` 装上预取 factory。返回是否真的装上（未启用时 False）。

    ⚠️ 必须在 `run_stream` 之前调用。

    第二个返回值不用 —— 调用方只需要知道装没装上（用于日志）。
    """
    if loop is None:
        return False
    if not prefetch_enabled():
        loop.prefetch_factory = None
        return False
    if client is None:
        loop.prefetch_factory = None
        return False
    loop.prefetch_factory = _make_prefetch_factory(client, str(user_id), scope)
    return True


async def selector_client_for(user_id: str, *, fallback: Optional[Any] = None):
    """按 `HLZ_MEMORY_SELECTOR_MODEL` 造一个**独立**的小模型 client。

    为什么必须是独立 client 而不是复用主模型：
    1. 成本 —— 主模型跑 10 次 vs 小模型跑 10 次，差一个数量级；
    2. 上下文 —— 选择器不需要主模型那套系统提示词和 MCP 工具，
       复用会把它们白白发一遍。

    `HLZ_MEMORY_SELECTOR_MODEL` 可以是：
      - 一个 llm config 的 id（`llm_xxx`）—— 用那条配置（含自己的端点和 key）；
      - 一个模型名（如 `qwen-turbo`）—— 用用户的默认配置的端点，只换 model 字段。

    **找不到用户配置时的兜底顺序**（这一条不写清就会表现为「配了却不生效」）：
      1. 用户的默认 LLM 配置；
      2. 全局 `llm.yaml`（`HLZ_LLM_*` / 配置文件）—— 单用户 / 本机部署常见，
         这类环境里 `user_llm_configs` 是空表；
      3. 调用方传进来的 `fallback`（通常是主模型 client 的 settings）。

    第 3 条是最后手段，**只在显式传入时**用 —— 不自动复用主模型，
    否则「必须指定独立小模型」这条成本保护就形同虚设。
    """
    from . import db
    from .config import llm_yaml_available, load_llm_settings
    from .llm import build_client_from_config

    want = selector_model()
    if not want:
        return None

    cfg: Optional[dict] = None
    if want.startswith("llm_"):
        try:
            cfg = await db.get_llm_config(user_id, want)
        except Exception:  # noqa: BLE001
            cfg = None
    if cfg is None:
        try:
            base = await db.get_default_llm_config(user_id)
        except Exception:  # noqa: BLE001
            base = None
        if base:
            cfg = dict(base)
            cfg["model"] = want
    if cfg is not None and cfg.get("api_key") and cfg.get("base_url"):
        try:
            return build_client_from_config(cfg, system_prompt="")
        except Exception:  # noqa: BLE001
            logger.warning("[memory] 选择器 client 构造失败（用户配置）", exc_info=True)

    # 兜底 2：全局 llm.yaml。单用户 / 本机部署里这是唯一来源。
    if llm_yaml_available():
        try:
            s = load_llm_settings()
            if s.api_key and s.base_url:
                return build_client_from_config({
                    "base_url": s.base_url, "api_key": s.api_key,
                    "model": want, "wire_api": s.wire_api,
                    "max_tokens": s.max_tokens,
                    "context_window": s.context_window,
                }, system_prompt="")
        except Exception:  # noqa: BLE001
            logger.warning("[memory] 选择器 client 构造失败（llm.yaml）", exc_info=True)

    # 兜底 3：调用方显式给的（见 docstring，不自动用）
    if fallback is not None:
        return fallback
    logger.info("[memory] 选择器已开启但找不到可用配置（用户配置与 llm.yaml 都不可用），"
                "本轮走关键词排序")
    return None

