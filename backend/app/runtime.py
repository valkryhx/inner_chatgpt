"""Process-level runtime: shared HistoryStore + per-user lazy RuntimeHub + stop registry."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from .agent import UserRuntime, probe_mcp_status
from .history_store import get_history_store, HistoryStore

import logging

logger = logging.getLogger(__name__)

_hub: Optional["RuntimeHub"] = None


def set_hub(hub: Optional["RuntimeHub"]) -> None:
    global _hub
    _hub = hub


def get_hub() -> Optional["RuntimeHub"]:
    return _hub


class RuntimeHub:
    def __init__(self) -> None:
        self.store: Optional[HistoryStore] = None
        # key = (user_id, model_key); model_key = session's LLM config id ("" = follow default)
        self._runtimes: dict[tuple[str, str], UserRuntime] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        # 活跃的子代理宿主：key = (user_id, session_id, generation_id, team_id)
        # 用户 id 必须参与键，防止只猜 session/generation 就能影响别人。
        self._subagent_hosts: dict[tuple[str, str, str, str], "object"] = {}
        # runtime 建立时间（monotonic），用于 MCP 状态的软 TTL 刷新。
        self._built_at: dict[tuple[str, str], float] = {}
        # 待重建标记：key = (user_id, model_key)。
        #
        # 场景：`refresh_mcp_status` 探测到连接已死，但此刻**有生成在跑**，
        # 不能立刻 `close()`（那会打断正在用的 toolset / LLM client）。
        # 于是把 key 记在这里，等下一次「没有活跃生成」时由 `get_user_runtime` 消费。
        #
        # 为什么必须有这个集合（2026-11-19 事故 §6.3）：
        # 原来那个分支直接 `return`，连探测都不做，于是「卡住的那一轮恰好就是
        # 有生成在跑」⇒ 永远走不到重建，形成死角。有了待重建标记，
        # 「不打断」和「最终会重建」才不矛盾。
        self._pending_rebuild: set[tuple[str, str]] = set()
        # 本代 ACP manager：key = (user_id, session_id, generation_id)
        # 与 subagent hosts 同构，用户 id 参与键，/stop 不能作用于别人的会话。
        self._acp_managers: dict[tuple[str, str, str], "object"] = {}
        # 跨实例失效监听：key = user_id。缓存里有这个用户的 runtime 才需要监听。
        self._invalidate_watchers: dict[str, object] = {}

    # ---------------- acp managers ----------------
    def register_acp_manager(self, user_id: str, session_id: str, generation_id: str,
                             manager: "object") -> None:
        self._acp_managers[(user_id, session_id, generation_id)] = manager

    def unregister_acp_manager(self, user_id: str, session_id: str, generation_id: str) -> None:
        self._acp_managers.pop((user_id, session_id, generation_id), None)

    def get_acp_manager(self, user_id: str, session_id: str, generation_id: str):
        return self._acp_managers.get((user_id, session_id, generation_id))

    async def stop_acp_generation(self, user_id: str, session_id: str, generation_id: str) -> int:
        """优雅停止该代的 ACP 会话；未知代或 manager 抛错都返回 0。"""
        mgr = self._acp_managers.get((user_id, session_id, generation_id))
        if mgr is None:
            return 0
        try:
            return await mgr.stop_generation()
        except Exception:
            return 0

    # ---------------- subagent hosts ----------------
    def register_subagent_host(self, user_id: str, session_id: str, generation_id: str,
                               team_id: str, host: "object") -> None:
        self._subagent_hosts[(user_id, session_id, generation_id, team_id)] = host

    def unregister_subagent_host(self, user_id: str, session_id: str, generation_id: str,
                                 team_id: str = "") -> None:
        self._subagent_hosts.pop((user_id, session_id, generation_id, team_id), None)

    def get_subagent_host(self, user_id: str, session_id: str, generation_id: str, team_id: str = ""):
        return self._subagent_hosts.get((user_id, session_id, generation_id, team_id))

    async def stop_subagent_generation(self, user_id: str, session_id: str, generation_id: str) -> int:
        """级联停止：只匹配该用户、该会话、该代的所有 host。"""
        keys = [k for k in self._subagent_hosts
                if k[0] == user_id and k[1] == session_id and k[2] == generation_id]
        hosts = [self._subagent_hosts[k] for k in keys]
        if not hosts:
            return 0
        n = 0
        for host in hosts:
            try:
                n += len(host.list_agents())
                await host.stop_all()
            except Exception:
                pass
        return n

        if host is None:
            return 0
        try:
            n = len(host.list_agents())
            await host.stop_all()
            return n
        except Exception:
            return 0

    async def startup(self) -> None:
        self.store = get_history_store()
        await self.store.init()

    async def shutdown(self) -> None:
        for host in list(self._subagent_hosts.values()):
            try:
                await host.close_all()
            except Exception:
                pass
        self._subagent_hosts.clear()
        for mgr in list(self._acp_managers.values()):
            try:
                await mgr.close_all()
            except Exception:
                pass
        self._acp_managers.clear()
        for key in list(self._runtimes.keys()):
            await self._runtimes[key].close()
        self._runtimes.clear()
        self.store = None

    async def get_user_runtime(self, user_id: str, model_id: str = "") -> UserRuntime:
        """Get/build runtime for (user, session-selected model)."""
        key = (user_id, model_id or "")
        # 消费「待重建」标记：上一次 refresh 探测到连接已死、但当时有生成在跑
        # 所以没敢重建（见 _pending_rebuild 的说明）。现在如果已经没有活跃生成，
        # 就把旧的换掉；否则继续不动（绝不打断正在跑的那一轮）。
        if key in self._pending_rebuild and not has_active_generation_for_user(user_id):
            self._pending_rebuild.discard(key)
            logger.info("消费待重建标记，重建 runtime user=%s model=%s", user_id, model_id)
            await self.invalidate(user_id)
        cur = self._runtimes.get(key)
        if cur is not None:
            return cur
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cur = self._runtimes.get(key)
            if cur is not None:
                return cur
            ur = UserRuntime()
            await ur.build(user_id, model_id or None)
            self._runtimes[key] = ur
            self._built_at[key] = time.monotonic()
            # 起跨实例失效监听：这个用户的缓存现在存在本进程里了，
            # 别的实例改配置时必须能让本进程清掉它。
            await self._watch_invalidate(user_id)
            return ur

    async def _watch_invalidate(self, user_id: str) -> None:
        """给这个用户挂一个跨实例失效监听（幂等）。没配 Redis 时空操作。

        ## 基线必须在这里**同步**读定

        踩过的坑：一开始把「读当前版本号」交给 watcher 的第一个轮询迭代，
        结果那 0~2s 内的广播被当成基线吞掉了 —— 用户在这 2 秒内改了配置，
        本实例永远收不到。测试里 100% 复现（`seen=1` 却从未清缓存）。

        所以改成：启动 watcher **之前**先 await 读一次版本作为基线。
        剩下的窗口只有「读完基线到 start 之间」的微秒级。
        """
        if user_id in self._invalidate_watchers:
            return
        try:
            from .cluster import invalidate as inv
        except Exception:  # noqa: BLE001
            return

        baseline = await inv.current_version(user_id)

        async def _on_change(uid: str) -> None:
            # 只清本地缓存，不再广播（否则会形成来回广播的环）
            logger.info("收到跨实例失效广播，清本地 runtime user=%s", uid)
            await self.invalidate(uid)
            self._invalidate_watchers.pop(uid, None)

        try:
            watcher = inv.InvalidateWatcher(user_id, _on_change)
            watcher.start(baseline=baseline)
            self._invalidate_watchers[user_id] = watcher
        except Exception:  # noqa: BLE001
            logger.debug("失效监听启动失败", exc_info=True)

    async def refresh_mcp_status(self, user_id: str, model_id: str = "",
                                 *, force: bool = False) -> list[dict]:
        """按软 TTL 刷新 MCP 连接状态；只有状态真的变了（或会话已死）才重建。

        为什么不是定时重建：重建会 close 掉旧的 toolset / LLM client，代价是
        重新连接全部 MCP server（可能好几秒）。状态没变时重建纯属浪费。

        为什么必须跳过活跃轮次：重建会把正在执行的那一轮的 toolset 关掉，
        导致工具调用失败。有生成在跑时直接返回旧状态，等下一轮再说。

        为什么不只看 manifest：**缓存里的连接可能已经死了**。MCP server 重启后，
        新连接的 probe 依然返回「已连接、12 工具」，与缓存 manifest 一致，
        于是判定「状态没变」不重建 —— 而旧 client 每次调用都 `Session terminated`，
        用户永久卡死（2026-11-10 实测）。所以先探测缓存连接本身的活性。
        """
        key = (user_id, model_id or "")
        ur = self._runtimes.get(key)
        if ur is None:
            return []
        if not force:
            built = self._built_at.get(key, 0.0)
            if time.monotonic() - built < _MCP_STATUS_TTL_S:
                return getattr(ur.toolset, "manifest", []) or []

        # ---- 有生成在跑：探测可以，重建不行 ----
        #
        # 原来这里直接 `return 缓存状态`，连探测都不做。后果是
        # **用户打开设置页看到的永远是假的「已连接」**，而卡住的那一轮
        # 恰恰就是「有生成在跑」⇒ 永远不重建，用户无法自救、也无法诊断
        # （2026-11-19 事故 §6.3）。
        #
        # 探测是只读的（`_toolset_is_live` 只调一个无副作用的探针工具，
        # 不走 MCP 配额，不 close 任何东西），所以完全可以做。
        # 真正的风险只有「重建」，那就把重建推迟。
        if has_active_generation_for_user(user_id):
            live = await _toolset_is_live(getattr(ur, "toolset", None))
            if live:
                # 连接活着：续期，省掉每次进设置页都探一遍
                self._built_at[key] = time.monotonic()
                return getattr(ur.toolset, "manifest", []) or []
            # 连接已死：不重建（会打断），但如实报告 + 记下待重建
            self._pending_rebuild.add(key)
            return _mark_dead(
                getattr(ur.toolset, "manifest", None) or [],
                "连接已无响应（探测超时或会话已断开）；已安排在对话结束后自动重连",
            )

        # 1) 先看缓存连接是否还活着（真实往返，比 list_tools 更严格）
        live = await _toolset_is_live(getattr(ur, "toolset", None))
        if not live:
            await self.invalidate(user_id)
            ur = await self.get_user_runtime(user_id, model_id)
            return getattr(ur.toolset, "manifest", []) or []

        # 2) 连接活着：再看配置侧有没有变化（server 增删/改 URL）
        fresh = await probe_mcp_status(user_id)
        current = getattr(ur.toolset, "manifest", []) or []
        if _same_manifest(current, fresh):
            # 状态没变：只续期，不重建。
            self._built_at[key] = time.monotonic()
            return current

        await self.invalidate(user_id)
        ur = await self.get_user_runtime(user_id, model_id)
        return getattr(ur.toolset, "manifest", []) or []

    async def invalidate(self, user_id: str) -> None:
        for key in list(self._runtimes.keys()):
            if key[0] == user_id:
                ur = self._runtimes.pop(key, None)
                self._built_at.pop(key, None)
                # 标记跟着 runtime 一起清：重建完就不需要它了，
                # 留着会让下一次 get_user_runtime 白白再 invalidate 一次。
                self._pending_rebuild.discard(key)
                if ur is not None:
                    await ur.close()

    async def invalidate_cluster(self, user_id: str) -> None:
        """清本地缓存 **并广播给其他实例**。

        ## 为什么需要

        改了 MCP / LLM 配置或装了 skill 时，只清本进程的缓存不够：
        别的实例手里还握着旧的 `UserRuntime`（含进程内 MCP 连接和烘焙好的
        系统提示词）。那些连接跨进程无法共享，只能**各自失效重建**。

        原来的兜底是 `MCP_STATUS_TTL_S`（60s）到期后的状态比对 ——
        意味着「改了配置最多一分钟才生效，而且可能不生效」。
        广播把这个延迟降到 2s 且确定生效。

        ## 谁调用

        `me.py` / `skills.py` 里所有原本调 `invalidate` 的地方都改调这个。
        `invalidate` 保留给内部使用（例如 `refresh_mcp_status` 发现连接死了
        要重建 —— 那是本进程的事，不该广播）。
        """
        await self.invalidate(user_id)
        try:
            from .cluster import invalidate as inv
            await inv.publish(user_id)
        except Exception:  # noqa: BLE001 - 广播失败不该让配置修改失败
            logger.debug("跨实例失效广播失败", exc_info=True)

    async def invalidate_all(self) -> int:
        """Drop every cached runtime so the next turn rebuilds system prompts.

        Needed after a change that affects ALL users — chiefly importing a
        shared/public skill, whose L1 index is baked into each user's system
        prompt at build time. Returns how many runtimes were dropped.
        """
        keys = list(self._runtimes.keys())
        dropped = 0
        for key in keys:
            ur = self._runtimes.pop(key, None)
            self._built_at.pop(key, None)
            self._pending_rebuild.discard(key)
            if ur is not None:
                dropped += 1
                try:
                    await ur.close()
                except Exception:
                    pass
        return dropped


# ---------------- stop registry ----------------
# Production keys include the authenticated owner.  Session IDs are globally
# unique, but owner scoping is still enforced so a caller can never act on a
# guessed foreign key.
_stop_events: dict[tuple[str, str, str], asyncio.Event] = {}
# The streaming response is an asyncio task.  A cooperative flag alone cannot
# unblock a task that is awaiting an LLM read or a tool dispatch, so keep the
# task reference too and cancel it explicitly from the stop endpoint.
_generation_tasks: dict[tuple[str, str, str], asyncio.Task] = {}
# A request that has acquired the DB lease but whose StreamingResponse has not
# started iterating yet is still live.  Do not mistake that tiny window for a
# stale lease when a second tab sends concurrently.
_generation_reservations: dict[tuple[str, str, str], float] = {}
# Stop can arrive during the pre-stream prelude (runtime build / compaction),
# before register_generation has captured the actual asyncio task.  Remember
# the explicit intent so register_generation returns an already-set event.
_stop_requested: dict[tuple[str, str, str], float] = {}
_STOP_INTENT_TTL_S = 300.0
# 跨实例停止监听协程：key 同上。生成结束时必须 cancel，否则每轮留一个
# 每秒打一次 Redis 的协程。
_remote_watchers: dict[tuple[str, str, str], asyncio.Task] = {}
# A reservation only bridges the tiny gap between accepting an HTTP request
# and the StreamingResponse task entering the generator.  If a client never
# consumes the response body, the reservation must not block the session
# forever.  Thirty seconds is far longer than that hand-off window.
_RESERVATION_TTL_S = 30.0


def _key(user_id: str, session_id: str, generation_id: str) -> tuple[str, str, str]:
    return (user_id, session_id, generation_id)


def _cleanup_expired_reservations() -> None:
    cutoff = time.monotonic() - _RESERVATION_TTL_S
    for key, created in list(_generation_reservations.items()):
        if created < cutoff:
            _generation_reservations.pop(key, None)
    intent_cutoff = time.monotonic() - _STOP_INTENT_TTL_S
    for key, created in list(_stop_requested.items()):
        if created < intent_cutoff:
            _stop_requested.pop(key, None)


def _is_live_key(key: tuple[str, str, str]) -> bool:
    """Return true only for a task that can still run, or a fresh hand-off."""
    task = _generation_tasks.get(key)
    if task is not None and not task.done():
        return True
    if key in _stop_events:
        # The generator owns the event and removes it in its finally block.
        # If it is gone while the task is already done, the lease is stale.
        return False
    created = _generation_reservations.get(key)
    if created is None:
        return False
    if time.monotonic() - created >= _RESERVATION_TTL_S:
        _generation_reservations.pop(key, None)
        return False
    return True


def has_active_generation(session_id: str, user_id: str = "") -> bool:
    _cleanup_expired_reservations()
    for key in set(_stop_events) | set(_generation_tasks) | set(_generation_reservations):
        owner, sid, _gid = key
        if sid != session_id:
            continue
        if user_id and owner != user_id:
            continue
        if _is_live_key(key):
            return True
    return False


# MCP 状态软 TTL：超过这个时间后，下一次读取状态会重新探测一次。
# 只用于「用户在看状态」，不参与对话轮次的 runtime 取用。
_MCP_STATUS_TTL_S = float(__import__("os").environ.get("MCP_STATUS_TTL_S", "60"))


def _same_manifest(a: list[dict], b: list[dict]) -> bool:
    """比较两次探测的连接状态；掉顺序或错误文案细节不算变化。"""
    def norm(items):
        return sorted(
            (str(i.get("name") or ""), bool(i.get("ok")), int(i.get("tool_count") or 0))
            for i in items
        )
    return norm(a) == norm(b)


def _mark_dead(manifest: list[dict], reason: str) -> list[dict]:
    """把 manifest 里的连接如实标成「已断开」，供设置页显示。

    只在「探测到死连接但因活跃生成不能立刻重建」时用。
    不这么做的后果（2026-11-19 事故）：用户打开设置页看到缓存的假「已连接」，
    完全无法判断卡在哪 —— 唯一的诊断入口给出错误信息。

    不修改传入对象（manifest 可能被别处共享），返回新列表。
    """
    out = []
    for item in manifest:
        row = dict(item)
        # 已经标记为失败的行不要覆盖它的具体原因（那信息更有用）
        if row.get("ok"):
            row["ok"] = False
            row["tool_count"] = 0
            row["error"] = reason
        out.append(row)
    if not out:
        # 缓存里一条都没有时，也要能表达「连接有问题」而不是空列表
        out = [{"name": "", "ok": False, "tool_count": 0, "error": reason, "tools": []}]
    return out


# 会话活性探测的超时。MCP server 卡死时不能把状态查询也拖住。
_LIVENESS_TIMEOUT_S = 8.0


async def _toolset_is_live(toolset) -> bool:
    """缓存里的 MCP 连接是否还能用（真实往返，不是 list_tools）。

    为什么必须探活：`list_tools` 只能证明「server 活着」，证明不了**这条连接**
    还活着。fastmcp 的 client 在 server 重启后不会自己恢复，而探测用的是新连接 ——
    于是 manifest 一致、TTL 判定「没变」、不重建，坏 client 永久留在缓存里。
    实测（2026-11-10）：用户每轮调用都是 `Session terminated`，只能重启后端。

    判定规则（保守，宁可放过不可错杀）：
    - 没有 toolset / 没有子连接 → 视为活（没有可探测的对象）；
    - 探针调用成功 → 活；
    - 抛传输类错误（session terminated / connection closed…）→ 死；
    - 抛业务类错误（参数、工具自身异常）→ 仍视为活（连接是通的）；
    - 超时 → 视为死（卡死的连接和死连接没区别，重建更安全）。

    探针只挑**无必填参数**的工具，避免因缺参数被服务端拒绝而误判。
    """
    if toolset is None:
        return True
    children = getattr(toolset, "_children", None) or [toolset]
    probe_targets = [c for c in children if getattr(c, "_client", None) is not None]
    if not probe_targets:
        return True

    from .mcp_client import _looks_session_dead

    for child in probe_targets:
        tool = _pick_liveness_probe(child)
        if tool is None:
            continue
        try:
            await asyncio.wait_for(child._client.call_tool(tool.name, {}),
                                   timeout=_LIVENESS_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                return False
            if _looks_session_dead(exc):
                return False
            # 业务错误：连接是通的，继续看下一个
            continue
    return True


# 只探这些名字形态的工具：健康检查类、只读列举类。绝不拿任意工具当探针 ——
# 有些 server 的首个无参工具可能是会写数据的动作，探活不能有副作用。
_SAFE_PROBE_TOKENS = (
    "health", "ping", "status", "version", "info", "time", "now",
    "list", "options", "dimensions", "schema", "describe", "capabilities",
)


def _pick_liveness_probe(child):
    """挑一个**只读且无必填参数**的探针工具；找不到就不探（视为活）。

    宁可放过（假活）也不能错杀或产生副作用：
    - 有必填参数 → 缺参会被服务端拒绝，误判为死；
    - 名字不像健康/列举类 → 可能有写副作用，不能当探针。
    """
    tools = list(getattr(child, "tools", []) or [])
    for t in tools:
        schema = t.input_schema or {}
        if schema.get("required"):
            continue
        low = (t.name or "").lower()
        if any(tok in low for tok in _SAFE_PROBE_TOKENS):
            return t
    return None


def has_active_generation_for_user(user_id: str) -> bool:
    """该用户是否有任何会话正在生成。重建 runtime 前必须跳开这种情况，
    否则会让正在跑的轮次用到一个被 close 掉的 toolset / client。"""
    _cleanup_expired_reservations()
    for key in set(_stop_events) | set(_generation_tasks) | set(_generation_reservations):
        if key[0] != user_id:
            continue
        if _is_live_key(key):
            return True
    return False


def is_generation_live(user_id: str, session_id: str, generation_id: str) -> bool:
    _cleanup_expired_reservations()
    return _is_live_key(_key(user_id, session_id, generation_id))


def reserve_generation(user_id: str, session_id: str, generation_id: str) -> None:
    """Mark an accepted request live before its stream task starts iterating."""
    _generation_reservations[_key(user_id, session_id, generation_id)] = time.monotonic()


def release_generation_reservation(user_id: str, session_id: str, generation_id: str) -> None:
    _generation_reservations.pop(_key(user_id, session_id, generation_id), None)


def register_generation(user_id: str, session_id: str, generation_id: str) -> asyncio.Event:
    key = _key(user_id, session_id, generation_id)
    ev = asyncio.Event()
    # Consume a tombstone only when the stream actually registers.  A stop can
    # arrive and call unregister_generation() before this function runs (client
    # aborts immediately after receiving headers); that must not erase intent.
    if _stop_requested.pop(key, None) is not None:
        ev.set()
    _stop_events[key] = ev
    _generation_reservations[key] = time.monotonic()
    task = asyncio.current_task()
    if task is not None:
        _generation_tasks[key] = task
    # 跨实例停止：挂一个后台协程监听 Redis。收到标记就 set 本地 Event，
    # 之后 `stopped()` 照旧读这个 Event —— 核心循环零改动。
    # 没配 Redis 时 watch() 返回 None，行为与迁移前完全一致。
    _start_stop_watch(user_id, session_id, generation_id, ev)
    return ev


def _start_stop_watch(user_id: str, session_id: str, generation_id: str,
                      ev: asyncio.Event) -> None:
    """起跨实例停止监听（尽力而为，失败不影响本地停止）。"""
    key = _key(user_id, session_id, generation_id)
    try:
        from .cluster import stopsignal
        watcher = asyncio.create_task(
            stopsignal.watch(user_id, session_id, generation_id, ev))
        _remote_watchers[key] = watcher
    except Exception:  # noqa: BLE001
        logger.debug("跨实例停止监听启动失败", exc_info=True)


async def _stop_watch_task(watcher: asyncio.Task) -> None:
    """关掉一个停止监听（先取回 StopWatcher 再 stop）。"""
    try:
        w = await watcher
        if w is not None:
            await w.stop()
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def unregister_generation(user_id: str, session_id: str, generation_id: str) -> None:
    key = _key(user_id, session_id, generation_id)
    _stop_events.pop(key, None)
    _generation_tasks.pop(key, None)
    _generation_reservations.pop(key, None)
    # 关掉跨实例停止监听：不关的话每轮留一个每秒打 Redis 的协程。
    watcher = _remote_watchers.pop(key, None)
    if watcher is not None:
        _spawn(_stop_watch_task(watcher))
    # 顺手清掉 Redis 里的停止标记（避免同 id 复用被误停）。
    # 尽力而为：失败只可能是 Redis 抖动，下次生成会重新覆盖。
    try:
        from .cluster import stopsignal
        _spawn(stopsignal.clear(user_id, session_id, generation_id))
    except Exception:  # noqa: BLE001
        pass
    # Do not discard a *pending* tombstone here; the pre-stream stop path calls
    # unregister_generation() before StreamingResponse first iterates, and the
    # later register_generation() must still observe the stop intent.  However,
    # once this generation actually registered (event exists / task captured),
    # unregister marks normal completion and the tombstone must be cleared so a
    # reused generation id in tests or a future retry does not inherit a stop.
    _stop_requested.pop(key, None)


def _spawn(coro) -> None:
    """起一个「发后不管」的后台协程，**没有运行中的 loop 时静默丢弃**。

    为什么需要：`unregister_generation` / `request_stop` 都是同步函数，
    但测试里它们可能在 `asyncio.run` 之外被调用（例如构造/清理阶段）。
    直接 `create_task` 会抛 `RuntimeError: no running event loop`，
    把一次清理变成一次崩溃。这类后台动作失败没有正确性影响 ——
    最坏情况是 Redis 里留一个会自己过期的标记。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        loop.create_task(coro)
    except Exception:  # noqa: BLE001
        pass


def request_stop(user_id: str, session_id: str, generation_id: str | None = None) -> int:
    n = 0
    for key, ev in list(_stop_events.items()):
        owner, sid, gid = key
        if owner != user_id or sid != session_id:
            continue
        if generation_id and gid != generation_id:
            continue
        ev.set()
        n += 1
    if generation_id:
        # Record intent even when the stream has not started registering yet.
        _stop_requested[_key(user_id, session_id, generation_id)] = time.monotonic()
        # 跨实例：把这个意图也发到 Redis。生成可能跑在**另一个实例**上，
        # 那边看不到本进程的 `_stop_events`。对方有一个 StopWatcher
        # 在轮询这个 key，收到就 set 它本地的 Event。
        try:
            from .cluster import stopsignal
            _spawn(stopsignal.publish(user_id, session_id, generation_id))
        except Exception:  # noqa: BLE001
            pass
    return n


def cancel_generation(user_id: str, session_id: str, generation_id: str) -> int:
    """Cancel the actual streaming task in addition to setting the stop flag.

    Cancellation is what makes stop reliable while the request is blocked in
    an LLM socket or a tool wait.  The event flag stays for code paths that
    still use cooperative checks.
    """
    key = _key(user_id, session_id, generation_id)
    ev = _stop_events.get(key)
    if ev is not None:
        ev.set()
    task = _generation_tasks.get(key)
    if task is None or task.done():
        # Pre-stream prelude: make sure register_generation sees the intent.
        _stop_requested[key] = time.monotonic()
        return 0
    task.cancel()
    return 1
