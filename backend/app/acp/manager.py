"""ACP 会话管理器：打开会话名额、并发配额、进程租约与停止级联。

设计文档：``docs/20261109-ACP-client驱动GenericAgent设计与实施.md`` §9。

职责边界：

- 会话状态机、事件映射、文本聚合在 :mod:`app.acp.session`；
- 本模块只管三件事：**能开几个**（打开会话上限 / 并发 prompt 配额）、
  **算谁的**（``ProcessLease`` 让既有的停止端点也能杀到 GA 进程）、
  **怎么停干净**（``cancel`` → 2s → 杀进程树，且同步路径不 await）。

``cwd`` 一律由服务端拼（spec §10.1），绝不接受模型输入。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import workspace
from ..process_registry import ProcessLease, register, unregister
from ..quota import QuotaManager, get_quota
from ..subagent.context import AgentContext
from ..subagent.events import EventBuffer
from .config import AcpSettings, build_child_env
from .session import GaSession
from .transport import kill_tree_now

__all__ = [
    "AcpLimitError",
    "AcpSessionManager",
    "AcpUnavailableError",
    "reset_preflight_cache",
]

logger = logging.getLogger(__name__)

# 拿并发名额最多等这么久（spec §9.3：第 5 个 ga_spawn 等 2s 后报并发上限）。
_QUOTA_WAIT_S = 2.0

# 预检子进程最多等这么久：只解析一次模块路径，正常是百毫秒级。
_PREFLIGHT_TIMEOUT_S = 15

# 预检结果按命令行缓存（跨 manager 实例复用：每轮都会新建一个 manager）。
# 成功结果永久缓存（装好就不变）；失败结果只缓存 _PREFLIGHT_FAIL_TTL_S 这么久的
# 「短 TTL」——坏安装不能让每一轮都 spawn 一次解释器（mount 每轮都会 await 预检，
# 并发轮次还会一起占满默认线程池），装好依赖后又能自动恢复、无需重启进程。
_PREFLIGHT_CACHE: dict[tuple[str, ...], str] = {}

# 失败缓存：(monotonic 时间戳, 错误文本)。存文本而不是异常对象，避免复用带
# 陈旧 traceback 的异常实例。
_PREFLIGHT_FAIL_CACHE: dict[tuple[str, ...], tuple[float, str]] = {}

# 失败结果的缓存时长（秒）；测试可 monkeypatch 成本模块属性。
_PREFLIGHT_FAIL_TTL_S = 30.0


def reset_preflight_cache() -> None:
    """清空预检的成功 / 失败缓存（测试隔离，也给运维一个手动重试的入口）。"""
    _PREFLIGHT_CACHE.clear()
    _PREFLIGHT_FAIL_CACHE.clear()


# ga_stop：先发 session/cancel，最多等这么久；仍未返回就硬杀进程树（spec §9.4）。
_STOP_GRACE_S = 2.0

# 硬杀之后再给在途 prompt 一点时间收敛成终态（进程已死，它只会立刻失败）。
_KILL_SETTLE_S = 2.0

# ga_wait 的 result_preview 长度。
_PREVIEW_CHARS = 400

# 终态：进入后 manager 不再改写状态（spec §9.2）。
_TERMINAL_STATUSES = frozenset({"completed", "errored", "stopped"})

_SPAWN_NOTE = "用 ga_wait / ga_result 收结果；本轮结束会停止未完成任务"


class AcpLimitError(RuntimeError):
    """打开会话数超过单用户上限（spec §9.3 的第二道闸）。"""


class AcpUnavailableError(RuntimeError):
    """GA 的 ACP bridge 不可用（预检没 import 到模块 / 自定义命令起不来）。"""


def _remember_preflight_failure(
    key: tuple[str, ...], error: AcpUnavailableError
) -> AcpUnavailableError:
    """把失败写进短 TTL 缓存，并把 error 原样返回，方便调用方 ``raise``。"""
    _PREFLIGHT_FAIL_CACHE[key] = (time.monotonic(), str(error))
    return error


@dataclass
class _Entry:
    """一条会话的运行期记账（``GaSession`` 本体放在 ``manager._sessions``）。"""

    session: GaSession
    lease: ProcessLease
    prompt_seq: int = 0
    task: asyncio.Task[None] | None = None
    # 当前这次 prompt 持有的并发名额；prompt 结束时必须释放。
    quota_stack: AsyncExitStack | None = None
    quota_lease: Any | None = None


class AcpSessionManager:
    """一个 generation 内的 GA 会话集合。

    典型生命周期由调用方持有：本轮开始时构造，本轮结束时 ``await close_all()``。
    ``ctx`` 决定 user / session / generation —— 所有会话都记在它的名下。
    """

    def __init__(
        self,
        *,
        ctx: AgentContext,
        buffer: EventBuffer,
        settings: AcpSettings,
        quota: QuotaManager | None = None,
    ) -> None:
        self.ctx = ctx
        self.settings = settings
        # 公开属性：测试 / 上层可替换（例如注入更严的 Limits）。
        self.quota: QuotaManager = quota or get_quota()
        self._buffer = buffer
        # agent_id -> GaSession（打开会话名额的计数口径，spec §9.3）。
        self._sessions: dict[str, GaSession] = {}
        # agent_id -> 运行期记账；与 _sessions 同步增删。
        self._entries: dict[str, _Entry] = {}
        # agent_id -> 最近一次 prompt 的 stopReason（``GaSession.snapshot()`` 不含它，
        # 但 ``prompt()`` 返回的 PromptResult 有，spec §7.2 的 ga_result 要透出）。
        self._stop_reasons: dict[str, str] = {}

    # ------------------------------------------------------------ 公开查询

    def has_active(self) -> bool:
        """是否有 in-flight prompt（空闲会话不算 active，spec §9.1）。"""
        return any(
            entry.task is not None and not entry.task.done()
            for entry in self._entries.values()
        )

    # ---------------------------------------------------------------- 预检

    def preflight(self) -> str | None:
        """检查 GA 的 ACP bridge 是否可定位；返回 bridge 文件路径。

        **同步**方法：只解析一次模块路径，正常是百毫秒级，失败也在 15s 内返回。

        - 自定义命令（不含 ``-m``）没有可探测的模块名 → 返回 ``None``，把问题留给
          真正启动时的可读错误；
        - ``<python> -m <module>`` 形式 → 用**同一个解释器**跑
          ``importlib.util.find_spec(<module>).origin``。**只解析路径、不 import
          目标模块**：真实 bridge 在 import 时会把 fd1 / ``sys.stdout`` 重定向到
          stderr（bridge:14-45），真的 import 就拿不到路径，健康安装会被误判成不可用。
          这样「没装 / 装错 Python / 父包缺失」仍然在 spawn 之前就能判出来
          （spec §9.5：绝不注入半成品会话）。

        成功结果按 ``tuple(command)`` 缓存并复用（每轮都会新建 manager，缓存放模块级）；
        失败结果按同样的 key 做**短 TTL** 负缓存（``_PREFLIGHT_FAIL_TTL_S``，默认 30s）：
        坏安装不会让每一轮都付一次解释器启动，装好依赖后自动恢复、无需重启进程。
        """
        command = list(self.settings.command)
        if "-m" not in command:
            return None
        index = command.index("-m")
        if index + 1 >= len(command):
            # 命令本身残缺（``-m`` 后面没模块名）：交给启动路径报错，预检不猜。
            return None
        module = command[index + 1]

        key = tuple(command)
        cached = _PREFLIGHT_CACHE.get(key)
        if cached is not None:
            return cached

        failed = _PREFLIGHT_FAIL_CACHE.get(key)
        if failed is not None:
            raised_at, message = failed
            if time.monotonic() - raised_at < _PREFLIGHT_FAIL_TTL_S:
                # 直接复用上次的结论，不再 spawn 探测子进程。
                raise AcpUnavailableError(message)
            _PREFLIGHT_FAIL_CACHE.pop(key, None)

        # 子进程环境与 GA 会话一致（白名单），并强制 UTF-8 输出：
        # 中文用户名下的安装路径在 GBK 控制台里会被解成乱码。
        env = build_child_env(self.settings)
        env["PYTHONIOENCODING"] = "utf-8"
        # ⚠️ 只解析模块路径，**绝不执行**目标模块：真实 bridge 在 import 时会把
        # fd1 / ``sys.stdout`` 重定向到 stderr（bridge:14-45），一旦真的 import，
        # 探测子进程的 stdout 就是空的，健康安装会被误判成不可用、ga_* 工具永远不注入。
        # ``find_spec`` 只加载父包（必要时），不执行目标模块本身。
        probe = (
            "import importlib.util as u; "
            f"s = u.find_spec({module!r}); "
            "print(s.origin if s else '')"
        )
        try:
            proc = subprocess.run(
                [command[0], "-c", probe],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_PREFLIGHT_TIMEOUT_S,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise _remember_preflight_failure(
                key,
                AcpUnavailableError(
                    f"GenericAgent ACP bridge 不可用: {type(exc).__name__}: {exc}"
                ),
            ) from exc

        path = (proc.stdout or "").strip()
        if proc.returncode != 0 or not path:
            # 注意：``find_spec`` 对「找不到的顶层模块」返回 None 而不是抛异常，
            # 所以 stdout 是空行、stderr 也是空的。这时必须自己把模块名写进错误，
            # 否则模型只会看到「无输出」而无法自救。
            detail = (proc.stderr or "").strip()[-800:] or (
                f"未找到模块 {module}；确认 GA 装在该 Python 环境（{command[0]}）里"
            )
            raise _remember_preflight_failure(
                key, AcpUnavailableError(f"GenericAgent ACP bridge 不可用: {detail}")
            )

        try:
            digest = _sha256_of(path)
        except OSError as exc:  # 路径解不出来 / 不可读：可用性不受影响，只丢摘要
            logger.warning("ACP 预检：无法计算 bridge 摘要（%s）：%s", path, exc)
        else:
            logger.info("ACP 预检通过：%s sha256=%s", path, digest)
        _PREFLIGHT_CACHE[key] = path
        return path

    # ---------------------------------------------------------------- 派活

    async def spawn(
        self,
        *,
        task: str,
        title: str = "",
        mode: str = "",
        timeout_s: float = 0.0,
    ) -> dict[str, Any]:
        """建会话 + 后台发一次 prompt，**立刻返回**（不阻塞主对话）。"""
        self.preflight()
        text = (task or "").strip()
        if not text:
            raise ValueError("task 不能为空")
        self._check_open_cap()

        stack = AsyncExitStack()
        # 名额按「有 in-flight prompt 的会话」计：拿不到就抛 QuotaExceeded，
        # 由上层工具转成 ``[工具执行错误] 并发上限``。
        quota_lease = await stack.enter_async_context(
            self.quota.acp_active(self.ctx.user_id, timeout_s=_QUOTA_WAIT_S)
        )

        session: GaSession | None = None
        lease: ProcessLease | None = None
        try:
            cwd = self._session_cwd()
            agent_id = f"ga_{uuid4().hex[:8]}"
            settings = replace(self.settings, mode=mode) if mode else self.settings
            session = GaSession(
                agent_id,
                title,
                text,
                settings=settings,
                cwd=str(cwd),
                env=build_child_env(settings),
                publish=self._publish,
            )
            await session.start()
            lease = ProcessLease(
                self.ctx.user_id, self.ctx.session_id, self.ctx.generation_id, agent_id
            )
            # 必须是 AsyncProcHandle（异步 wait），不能塞裸 Popen：既有的
            # process_registry._kill_tree 会 await proc.wait()（spec §9.4）。
            lease.proc = session.process_handle
            await register(lease)
        except BaseException as exc:
            if lease is not None:
                await _quietly(unregister(lease))
            if session is not None:
                await _quietly(session.close())
            quota_lease.release()
            await _quietly_aclose(stack)
            if not isinstance(exc, Exception):
                # CancelledError / KeyboardInterrupt / SystemExit 原样上抛。
                raise
            # bridge 起不来时给一个可读错误，绝不注入半成品会话（spec §9.5）。
            raise RuntimeError(
                f"启动 GenericAgent 失败：{type(exc).__name__}: {exc}"
            ) from exc

        entry = _Entry(
            session=session,
            lease=lease,
            prompt_seq=1,
            quota_stack=stack,
            quota_lease=quota_lease,
        )
        self._sessions[session.agent_id] = session
        self._entries[session.agent_id] = entry
        entry.task = asyncio.create_task(
            self._run_prompt(entry, text, self._prompt_timeout(timeout_s))
        )
        return {"agent_id": session.agent_id, "status": "running", "note": _SPAWN_NOTE}

    async def send(self, agent_id: str, message: str) -> dict[str, Any]:
        """对同一会话追加一次 prompt；必须有没在跑的 prompt（一次一张卡片）。"""
        entry = self._session_or_raise(agent_id)
        text = (message or "").strip()
        if not text:
            raise ValueError("message 不能为空")
        if entry.task is not None and not entry.task.done():
            raise RuntimeError(
                f"会话 {agent_id} 仍有进行中的 prompt，先用 ga_wait 收结果"
            )

        stack = AsyncExitStack()
        quota_lease = await stack.enter_async_context(
            self.quota.acp_active(self.ctx.user_id, timeout_s=_QUOTA_WAIT_S)
        )
        entry.prompt_seq += 1
        # 新 prompt 的 stopReason 还没到，先清掉上一次的，避免读到陈旧值。
        self._stop_reasons.pop(agent_id, None)
        # 新卡片要展示新的任务书（GaSession.prompt 发 subagent_start 时读 self.task）。
        entry.session.task = text
        entry.quota_stack = stack
        entry.quota_lease = quota_lease
        entry.task = asyncio.create_task(
            self._run_prompt(entry, text, self.settings.prompt_timeout_s)
        )
        return {"agent_id": agent_id, "status": "running"}

    # ---------------------------------------------------------------- 收结果

    async def wait(
        self,
        agent_id: str | None,
        *,
        timeout_s: float = 60.0,
        since_seq: int = 0,
    ) -> dict[str, Any]:
        """等一个会话（``None`` = 全部）的 prompt 结束；超时**不杀** GA。"""
        entries = self._select(agent_id)
        tasks = {
            entry.task
            for entry in entries.values()
            if entry.task is not None and not entry.task.done()
        }
        timed_out = False
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=max(0.0, float(timeout_s)))
            timed_out = bool(pending)
        return {
            "agents": [
                self._wait_row(aid, entry, since_seq) for aid, entry in entries.items()
            ],
            "timed_out": timed_out,
        }

    async def result(
        self,
        agent_id: str,
        *,
        max_chars: int = 20000,
        full: bool = False,
        close: bool = False,
    ) -> dict[str, Any]:
        """读结果；``full=True`` 取全量文本，``close=True`` 顺手释放打开名额。"""
        entry = self._session_or_raise(agent_id)
        snap = entry.session.snapshot()
        text = snap["full_text"] if full else snap["result"]
        payload: dict[str, Any] = {
            "agent_id": snap["agent_id"],
            "status": snap["status"],
            "prompts": snap["prompts"],
            "result": text,
            "stop_reason": self._stop_reasons.get(agent_id, ""),
            "tool_calls": snap["tool_calls"],
            "error": snap["error"],
        }
        limit = max(0, int(max_chars))
        if limit and len(text) > limit:
            payload["result"] = text[:limit]
            payload["truncated"] = True
            payload["result_chars"] = len(text)
            payload["note"] = (
                f"结果已截断到 {limit} 字符（全文 {len(text)} 字符）；"
                "需要全文请传 full=true 或调大 max_chars"
            )
        if close:
            await self._close_session(agent_id)
            payload["closed"] = True
            # 关会话会把仍在跑的 prompt 收敛成终态，状态以关完之后的为准。
            payload["status"] = entry.session.snapshot()["status"]
        return payload

    # ---------------------------------------------------------------- 停止

    async def stop(self, agent_id: str | None) -> dict[str, Any]:
        """cancel → 最多等 2s → 硬杀进程树；``None`` 表示全部（并发停）。"""
        entries = self._select(agent_id)
        await self._stop_entries(entries)
        return {"stopped": list(entries)}

    async def stop_all(self) -> int:
        """停掉所有会话，返回停掉的会话数。"""
        return len((await self.stop(None))["stopped"])

    async def stop_generation(self) -> int:
        """停掉本 generation 的全部会话（用户点停止的优雅路径，spec §9.4）。"""
        entries = {
            aid: entry
            for aid, entry in self._entries.items()
            if entry.lease.generation_id == self.ctx.generation_id
        }
        await self._stop_entries(entries)
        return len(entries)

    def stop_all_now(self) -> None:
        """同步硬杀所有会话；**不 await**，供 cancel-safe 的清理路径调用。

        不能 ``await session.stop()``，所以用 ``GaSession.mark_stopped()``：
        它同步置上 stop-requested，让在途 prompt 因管道关闭失败时收敛成
        ``stopped`` 而不是 ``errored``（spec §9.4：用户停止 = stopped）。
        """
        for entry in list(self._entries.values()):
            session = entry.session
            session.mark_stopped()
            kill_tree_now(session.process_handle)

    async def close_all(self) -> None:
        """本轮结束的收尾：停掉 → 关传输 → 交还租约 → 清表。"""
        await self.stop_all()
        entries = list(self._entries.items())
        self._entries.clear()
        self._sessions.clear()
        self._stop_reasons.clear()
        for _, entry in entries:
            await _quietly(entry.session.close())
            await _quietly(unregister(entry.lease))

    # ---------------------------------------------------------------- 内部

    async def _run_prompt(self, entry: _Entry, text: str, timeout_s: float) -> None:
        """包一层任务：记 stopReason，无论成败都释放名额、清掉 task 引用。"""
        try:
            result = await entry.session.prompt(
                text, prompt_seq=entry.prompt_seq, timeout_s=timeout_s
            )
            self._stop_reasons[entry.session.agent_id] = result.stop_reason
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - GaSession 已收敛成终态并发过 subagent_end
            pass
        finally:
            try:
                await self._release_quota(entry)
            finally:
                entry.task = None

    async def _release_quota(self, entry: _Entry) -> None:
        """释放这次 prompt 的并发名额（幂等：先同步 release，再关 stack）。"""
        stack, lease = entry.quota_stack, entry.quota_lease
        entry.quota_stack = None
        entry.quota_lease = None
        if lease is not None:
            # 先同步释放：即使下面的 aclose 被取消 / 失败，名额也不会漏。
            lease.release()
        if stack is not None:
            await _quietly_aclose(stack)

    async def _stop_entries(self, entries: dict[str, _Entry]) -> None:
        """并发停多条会话：一条慢会话不能把其它停机会话和 SSE 一起拖住。"""
        if not entries:
            return
        await asyncio.gather(
            *(self._stop_entry(entry) for entry in entries.values()),
            return_exceptions=True,
        )

    async def _stop_entry(self, entry: _Entry) -> None:
        session = entry.session
        task = entry.task
        try:
            await session.stop()  # 发 session/cancel，并把状态标成 stopped
        except Exception:  # noqa: BLE001 - 停不下来也必须继续走硬杀
            pass
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=_STOP_GRACE_S)
            if not task.done():
                # 同步杀树最多阻塞 ~2s；绝不能占住事件循环（SSE 是所有用户共用的）。
                await asyncio.to_thread(kill_tree_now, session.process_handle)
                # 进程已死，在途 prompt 会以 AcpTransportClosed 立刻收尾。
                await asyncio.wait({task}, timeout=_KILL_SETTLE_S)
        if session.status not in _TERMINAL_STATUSES:
            session.status = "stopped"

    async def _close_session(self, agent_id: str) -> None:
        """停掉并释放一条会话（``ga_result close=true`` / 超限回收用）。"""
        entry = self._entries.get(agent_id)
        if entry is None:
            return
        await self._stop_entry(entry)
        try:
            await entry.session.close()
        finally:
            self._entries.pop(agent_id, None)
            self._sessions.pop(agent_id, None)
            self._stop_reasons.pop(agent_id, None)
            await _quietly(unregister(entry.lease))

    def _check_open_cap(self) -> None:
        cap = max(1, int(self.settings.max_open_user))
        if len(self._sessions) >= cap:
            raise AcpLimitError(
                f"打开会话数已达上限 {cap}；先 ga_result close=true 释放，"
                "或用 ga_stop 停掉不再需要的会话"
            )

    def _select(self, agent_id: str | None) -> dict[str, _Entry]:
        if agent_id is None:
            return dict(self._entries)
        return {agent_id: self._session_or_raise(agent_id)}

    def _session_or_raise(self, agent_id: str) -> _Entry:
        entry = self._entries.get(agent_id)
        if entry is None:
            raise KeyError(f"未知的 GA 会话: {agent_id}")
        return entry

    def _session_cwd(self) -> Path:
        """cwd 只由服务端拼（spec §10.1）：用户 workspace，可再隔离到子目录。"""
        cwd = Path(workspace.user_workspace(self.ctx.user_id))
        if self.settings.workdir_mode == "subdir":
            cwd = cwd / ".ga" / self.ctx.generation_id
        cwd.mkdir(parents=True, exist_ok=True)
        return cwd

    def _prompt_timeout(self, timeout_s: float) -> float:
        value = float(timeout_s or 0.0)
        return value if value > 0 else self.settings.prompt_timeout_s

    def _publish(self, event: dict[str, Any]) -> None:
        self._buffer.put(event)

    @staticmethod
    def _wait_row(agent_id: str, entry: _Entry, since_seq: int) -> dict[str, Any]:
        snap = entry.session.snapshot()
        events = entry.session.tail(since_seq)
        # result / full_text 只在 prompt 结束（_finish_prompt）时才拼好；
        # 进行中的会话必须退回事件日志，否则 ga_wait 的「先看中间文本」永远是空。
        preview = snap["result"] or snap["full_text"]
        if not preview:
            preview = "".join(str(event.get("text") or "") for event in events)
        return {
            "agent_id": agent_id,
            "status": snap["status"],
            "last_seq": snap["last_seq"],
            "events_tail": events,
            "result_preview": preview[:_PREVIEW_CHARS],
        }


# --------------------------------------------------------------- 小工具


def _sha256_of(path: str) -> str:
    """算文件摘要（分块读，避免把大文件整读进内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _quietly(coro: Any) -> None:
    """跑一个清理协程，吞掉异常（清理路径不能反过来打断主流程）。"""
    try:
        await coro
    except BaseException:  # noqa: BLE001 - 清理失败只影响清理本身
        pass


async def _quietly_aclose(stack: AsyncExitStack) -> None:
    """关闭退出栈；名额已单独释放过，这里失败不再影响收尾。"""
    try:
        await stack.aclose()
    except BaseException:  # noqa: BLE001
        pass
