"""Sandbox Manager：对外唯一门面。

设计文档 §6.1、§6.5、§7.1、§7.2。

上层（``builtin_tools``）只调这里，不直接碰 ``pool`` / ``runtime``：
- ``exec_code``    : code_run 的容器路径
- ``write_file``   : file_write 的容器路径
- ``patch_file``   : file_patch 的容器路径
- ``stop``         : /stop 级联
- ``start`` / ``shutdown`` : lifespan 挂载

**降级语义（铁律 2：绝不静默降级）**：
- ``auto``   : 容器不可用时退回进程内，但返回值首行写明「本次未隔离」
- ``strict`` : 容器不可用时直接失败

本模块**不实现**「要不要走沙箱」的判定 —— 那是 ``policy.should_use_sandbox``。
"""
from __future__ import annotations

import asyncio
import logging
import time

from .config import SandboxConfig, get_config
from .errors import SandboxError, SandboxExecError, SandboxTimeout, SandboxUnavailable
from . import policy, probe
from .pool import ContainerPool, get_pool, reap_loop
from .runtime import WORKSPACE_MOUNT

logger = logging.getLogger(__name__)

# 降级标记（会出现在工具结果首行，满足"绝不静默"）
DEGRADED_PREFIX = "[sandbox] 本次未隔离：{reason}（当前模式 auto）"

# 容器内 shell 支持的类型（设计文档 §10.1）
_PY_TYPES = {"python", "py"}
_SH_TYPES = {"bash", "sh", "shell"}


def _mount_root(user_id: str) -> str:
    """容器**唯一**挂载的宿主目录：该用户的 workspace 根。

    ⚠️ 这是多智能体正确性的关键约束，不要改成 `write_root`。

    为什么：容器是按 `user_id` 一个（`pool._entries` 的 key 是 user_id），
    而子代理各有不同的 `write_root`（`.subagents/<session>/<agent>`）。
    如果容器挂的是某个具体的 write_root，那么：
      - 主 agent 建容器时挂根目录 → 子代理的脚本写到子目录下，
        容器里 `/ws/.hlz_tmp/` 找不到（2026-11-18 真机复现：
        `can't open file '/ws/.hlz_tmp/xxx.py'`）
      - 子代理建容器时挂子目录 → 主 agent 反而看不到自己的文件

    正确做法：容器**始终**挂用户根目录到 `/ws`，
    `write_root` 变成容器内的**子路径**（`/ws/.subagents/<sess>/<agent>`）。
    这样所有 agent 共享一个挂载点、一个容器，且各自目录互不干扰。
    """
    from ..workspace import user_workspace
    return str(user_workspace(user_id))


def _container_subpath(user_id: str, root: str = "") -> str:
    """把 ``write_root`` 换算成容器内的相对路径（相对 /ws）。

    ``""`` -> ``""``（就是 /ws 本身）
    ``.subagents/s/a1`` -> ``.subagents/s/a1``
    """
    if not (root or "").strip():
        return ""
    return root.strip().strip("/")


def _host_workspace(user_id: str, root: str = "") -> str:
    """解析宿主的 workspace 绝对路径（与进程内读工具同一套逻辑）。

    注意与 ``_mount_root`` 的区别：这里**带 write_root**，用于把文件
    真正落到子代理自己的目录；而容器挂载点永远用 ``_mount_root``。
    """
    from ..workspace import write_root_for
    return str(write_root_for(user_id, root))


class SandboxManager:
    """门面。进程内单例，通过 ``get_manager()`` 取用。"""

    def __init__(self, cfg: SandboxConfig | None = None,
                 pool: ContainerPool | None = None) -> None:
        self.cfg = cfg or get_config()
        self.pool = pool or get_pool()
        self._reaper: asyncio.Task | None = None

    # ---------------- 生命周期 ----------------

    async def start(self) -> tuple[bool, str]:
        """启动自检 + 清扫残留 + 起后台回收协程。返回 (ok, reason)。"""
        if not self.cfg.enabled:
            logger.info("沙箱未启用（HLZ_SANDBOX_MODE=off）")
            return True, ""

        ok, reason = probe.check(self.cfg)
        if not ok:
            logger.warning("沙箱自检失败：%s", reason)
            return False, reason

        n = await asyncio.to_thread(probe.cleanup_orphans)
        if n:
            logger.info("启动时清理了 %d 个残留沙箱容器", n)

        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(reap_loop(60.0))
            logger.info("沙箱 TTL 回收协程已启动（每 60s 扫一次）")
        return True, ""

    async def shutdown(self) -> int:
        """销毁所有容器并停掉回收协程。"""
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reaper = None
        return await self.pool.shutdown()

    # ---------------- 降级判断 ----------------

    def _degrade_reason(self) -> str | None:
        """容器当前是否可用；不可用返回原因。

        只在 ``auto`` 模式下用于降级；``strict`` 会把它变成异常。
        """
        ok, reason = probe.check(self.cfg)
        return None if ok else reason

    def _handle_unavailable(self, reason: str) -> str:
        """按档位处理「容器不可用」。返回降级说明文本（strict 下抛错）。"""
        if self.cfg.mode == "strict":
            raise SandboxUnavailable(reason)
        logger.warning("沙箱降级（mode=auto）：%s", reason)
        return DEGRADED_PREFIX.format(reason=reason)

    # ---------------- 可观测性 ----------------

    def _log_routed(self, tool: str, user_id: str, target: str, detail: str = "") -> None:
        """记录一次分流去向。

        沙箱生效时**工具结果和用户界面完全看不出来** —— `code_run` 的输出一样、
        `file_write` 的文案一样。没有这条日志，「到底走没走沙箱」只能靠猜，
        自测和线上排查都会失去判据。

        用固定前缀 ``沙箱分流`` 便于 grep：
            grep '沙箱分流' backend/data/logs/*.log
        """
        logger.info("沙箱分流 tool=%s user=%s -> %s%s",
                    tool, user_id, target, f"（{detail}）" if detail else "")

    # ---------------- code_run ----------------

    async def exec_code(
        self,
        user_id: str,
        code: str,
        *,
        code_type: str = "python",
        timeout: float = 60.0,
        root: str = "",
        session_id: str = "",
        generation_id: str = "",
        agent_id: str = "root",
    ) -> tuple[bool, str, str]:
        """执行代码。返回 ``(handled, output, marker)``。

        ``handled=True``  表示容器接管了这次调用，``output`` 是最终工具结果；
        ``handled=False`` 表示调用方应退回进程内路径（仅 ``auto`` 降级时出现，
        此时 ``output`` 是"未隔离"提示，调用方应把它拼在进程内结果前面）。

        这个签名让 ``builtin_tools`` 的改动保持最小（§7.1）。
        """
        reason = self._degrade_reason()
        if reason is not None:
            note = self._handle_unavailable(reason)
            return False, note, ""

        ctype = (code_type or "python").lower()
        if ctype in ("powershell", "ps1", "pwsh"):
            # 明确报错，不静默降级成 python（设计文档 §10.1）
            msg = ("[sandbox] powershell 在沙箱内不可用（容器镜像里没有 PowerShell）；"
                   "请改用 code_type=python 或 sh。")
            return True, msg, ""

        host_ws = _host_workspace(user_id, root)      # 文件真正落哪里（含 write_root）
        mount = _mount_root(user_id)                  # 容器挂哪个（永远是根）
        sub = _container_subpath(user_id, root)       # 容器内子路径
        entry = None
        try:
            entry = await self.pool.acquire(user_id, mount)
            timeout_s = min(float(timeout or 60), self.cfg.exec_timeout_s)
            self._log_routed("code_run", user_id, f"container {entry.container_id[:12]}",
                             f"{ctype}, timeout={timeout_s:.0f}s"
                             + (f", 子目录={sub}" if sub else ""))
            if ctype in _PY_TYPES:
                res = await self.pool.runtime.exec_python(
                    entry.container_id, code, timeout_s=timeout_s,
                    host_workspace=host_ws,    # 脚本落盘位置（含 write_root）
                    container_workspace=mount,  # 容器里 /ws 对应的宿主根
                    # cwd 也要跟着子目录走：脚本里 `open("a.txt","w")` 这类
                    # 相对路径必须落在子代理自己的目录，而不是用户根目录。
                    cwd=f"{WORKSPACE_MOUNT}/{sub}" if sub else WORKSPACE_MOUNT,
                )
            elif ctype in _SH_TYPES:
                res = await self.pool.runtime.exec_shell(
                    entry.container_id, code, timeout_s=timeout_s,
                    cwd=f"{WORKSPACE_MOUNT}/{sub}" if sub else WORKSPACE_MOUNT,
                )
            else:
                return True, f"[sandbox] 不支持的类型: {code_type}", ""
            return True, res.stdout, res.marker
        except SandboxTimeout as exc:
            return True, str(exc), ""
        except SandboxUnavailable as exc:
            note = self._handle_unavailable(exc.reason)
            return False, note, ""
        except SandboxError as exc:
            # 容器在但执行失败：这是真错误，不降级（降级会掩盖问题）
            logger.warning("容器内执行失败", exc_info=True)
            return True, f"[sandbox] {exc}", ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("沙箱执行异常", exc_info=True)
            if self.cfg.mode == "strict":
                return True, f"[sandbox] {exc.__class__.__name__}: {exc}", ""
            note = self._handle_unavailable(f"{exc.__class__.__name__}: {exc}")
            return False, note, ""
        finally:
            if entry is not None:
                await self.pool.release(entry)

    # ---------------- file_write / file_patch ----------------

    async def write_file(
        self,
        user_id: str,
        path: str,
        content: str,
        *,
        mode: str = "overwrite",
        root: str = "",
        registry: object | None = None,
    ) -> tuple[bool, str]:
        """容器内写文件。返回 ``(handled, text)``。

        ⚠️ **所有校验仍在宿主进程做**（设计文档 §7.2）：
        - ``safe_workspace_path`` 路径越界；
        - 写前必读门禁（``registry.check``）；
        - ``mode`` 合法性。
        容器只负责最终落盘 —— 容器内再校验是纵深，不是替代。
        """
        reason = self._degrade_reason()
        if reason is not None:
            return False, self._handle_unavailable(reason)

        from ..workspace import PathSecurityError, display_path, safe_workspace_path
        try:
            target = safe_workspace_path(user_id, path, root=root)
        except PathSecurityError as exc:
            return True, f"错误：{exc}"

        mode = (mode or "overwrite").lower()
        if mode not in ("overwrite", "append", "prepend"):
            return True, f"错误：不支持的 mode: {mode}（overwrite/append/prepend）"
        if mode == "overwrite" and target.exists() and registry is not None:
            ok, why = registry.check(target)
            if not ok:
                return True, f"错误：{why}"

        target.parent.mkdir(parents=True, exist_ok=True)
        text = content if isinstance(content, str) else str(content)
        existing = ""
        if mode != "overwrite" and target.exists():
            try:
                existing = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                existing = ""
        if mode == "append":
            new_text = existing + text
        elif mode == "prepend":
            new_text = text + existing
        else:
            new_text = text

        import pathlib
        host_ws = _host_workspace(user_id, root)      # 含 write_root，文件落这里
        mount = _mount_root(user_id)                  # 容器挂这里（永远是根）
        # 容器内路径必须相对**挂载根**算，不是相对 host_ws ——
        # 否则子代理的 write_root 会被算两遍（/ws/.subagents/.../.subagents/...）
        rel = target.relative_to(pathlib.Path(mount)).as_posix()
        remote = f"{WORKSPACE_MOUNT}/{rel}"

        code = _write_py(remote, new_text)
        entry = None
        try:
            entry = await self.pool.acquire(user_id, mount)
            self._log_routed("file_write", user_id, f"container {entry.container_id[:12]}",
                             f"{rel}, mode={mode}")
            res = await self.pool.runtime.exec_python(
                entry.container_id, code, timeout_s=60.0,
                host_workspace=host_ws, container_workspace=mount,
            )
            if res.exit_code != 0:
                return True, f"错误：写入失败（exit={res.exit_code}）\n{res.stdout}"
            # 文案与进程内路径完全一致（设计文档 §7.2 契约不变），
            # 用相对路径 —— 绝对宿主路径会在前端暴露 OS 与目录结构。
            return True, (f"已写入 {display_path(user_id, target)}"
                          f"（{len(new_text)} 字符，mode={mode}）")
        except SandboxUnavailable as exc:
            return False, self._handle_unavailable(exc.reason)
        except SandboxError as exc:
            return True, f"[sandbox] {exc}"
        finally:
            if entry is not None:
                await self.pool.release(entry)

    async def patch_file(
        self,
        user_id: str,
        path: str,
        old_content: str,
        new_content: str,
        *,
        root: str = "",
        registry: object | None = None,
    ) -> tuple[bool, str]:
        """容器内做 old→new 替换。返回 ``(handled, text)``。

        校验全在宿主进程：路径、文件存在、写前必读门禁、唯一性判定。
        **唯一性判定也在这里做**（不用容器内的 count），
        这样错误文案与进程内路径完全一致。
        """
        reason = self._degrade_reason()
        if reason is not None:
            return False, self._handle_unavailable(reason)

        from ..workspace import PathSecurityError, display_path, safe_workspace_path
        try:
            target = safe_workspace_path(user_id, path, root=root)
        except PathSecurityError as exc:
            return True, f"错误：{exc}"
        if not target.is_file():
            return True, f"错误：文件不存在: {path}"
        if registry is not None:
            ok, why = registry.check(target)
            if not ok:
                return True, f"错误：{why}"
        if not old_content:
            return True, "错误：old_content 不能为空"
        try:
            full = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return True, f"错误：读取失败: {exc}"
        occurrences = full.count(old_content)
        if occurrences == 0:
            return True, "错误：未找到匹配的旧文本块。请先用 file_read 确认当前内容，再分小段 patch。"
        if occurrences > 1:
            return True, (f"错误：找到 {occurrences} 处匹配，无法确定唯一位置。"
                          "请提供更长、更具体的上下文。")

        updated = full.replace(old_content, new_content, 1)

        import pathlib
        host_ws = _host_workspace(user_id, root)      # 含 write_root
        mount = _mount_root(user_id)                  # 容器挂载根
        rel = target.relative_to(pathlib.Path(mount)).as_posix()
        remote = f"{WORKSPACE_MOUNT}/{rel}"

        code = _write_py(remote, updated)
        entry = None
        try:
            entry = await self.pool.acquire(user_id, mount)
            self._log_routed("file_patch", user_id, f"container {entry.container_id[:12]}",
                             f"{rel}, 1 处替换")
            res = await self.pool.runtime.exec_python(
                entry.container_id, code, timeout_s=60.0,
                host_workspace=host_ws, container_workspace=mount,
            )
            if res.exit_code != 0:
                return True, f"错误：修改失败（exit={res.exit_code}）\n{res.stdout}"
            return True, f"已局部修改 {display_path(user_id, target)}（替换 1 处）"
        except SandboxUnavailable as exc:
            return False, self._handle_unavailable(exc.reason)
        except SandboxError as exc:
            return True, f"[sandbox] {exc}"
        finally:
            if entry is not None:
                await self.pool.release(entry)

    # ---------------- 停止 ----------------

    async def stop_user(self, user_id: str) -> int:
        """杀掉该用户容器内的进程（不销毁容器）。返回动作数。"""
        entry = self.pool.get_entry(user_id)
        if entry is None:
            return 0
        try:
            n = await asyncio.to_thread(
                self.pool.runtime.exec_shell_sync_killall, entry.container_id
            )
            return n
        except Exception:  # noqa: BLE001
            logger.warning("容器内杀进程失败", exc_info=True)
            return 0


# ---------------- 生成容器内执行的脚本 ----------------

def _write_py(remote: str, content: str) -> str:
    """生成容器内**整文件覆盖写**的 Python 脚本。

    内容通过 base64 传递，避免引号/换行/编码在拼接时出问题。
    ``append`` / ``prepend`` 的语义由调用方在宿主侧算好最终文本
    （与进程内 ``_write_impl`` 完全一致的做法，且能复用同一套错误文案）。
    """
    import base64
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return f'''
import base64, pathlib
target = pathlib.Path({remote!r})
target.parent.mkdir(parents=True, exist_ok=True)
data = base64.b64decode({payload!r})
with open(target, "wb") as fh:
    fh.write(data)
'''



# ---------------- 单例 ----------------

_manager: SandboxManager | None = None


def get_manager(*, reload: bool = False) -> SandboxManager:
    global _manager
    if _manager is None or reload:
        _manager = SandboxManager()
    return _manager