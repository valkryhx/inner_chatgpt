"""bwrap 执行器。

## 命令里的每一条参数都有实测依据

见 `docs/probes/20261119-bwrap可行性实测.sh`（25 项）。改这里的参数前先跑那个探针。

| 参数 | 不加会怎样（实测） |
| --- | --- |
| `--unshare-all` | 容器内无 CAP_SYS_ADMIN，直接 `Operation not permitted` |
| `--clearenv` | app 的 `DB_PASSWORD` / `OPENAI_API_KEY` 原样传进沙箱 |
| `--cap-drop ALL` | `chroot` 能成功（实测）；drop 后 EPERM |
| 最小挂载集 | `--ro-bind / /` 时读得到 app 的 `.env` |
| `--tmpfs /etc` | bwrap 自动建的 `/etc` 父目录**可写**，`/etc/hosts` 写得进去 |
| `--bind <用户目录> /ws` | bind 父目录会跨租户泄漏 |
| `prlimit` | bwrap 自己**不做**资源限制（`--help` 里没有内存/CPU 参数） |

## 三条已知限制（不是 bug，是 bwrap 的性质）

1. **没有内核攻击面隔离**：共享宿主内核。方案已接受（内部可信用户，§2.5.3）。
2. **`RLIMIT_NPROC` 在 root 下失效**：内核不对 root 执行。所以要靠**容器本身非 root**。
3. **`prlimit --as` 是 per-process**：不是 cgroup 组总量，`NPROC × AS` 才是真实上限。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
import time
from pathlib import Path
from typing import Callable, Optional

from .base import ExecResult

logger = logging.getLogger(__name__)

# 最小挂载集：只给运行 Python 必需的目录。
# **不要**改成 `--ro-bind / /`——那会把 app 源码和 .env 暴露给沙箱（实测）。
_RO_BINDS = ("/usr", "/lib", "/lib64", "/bin", "/sbin")

DEFAULT_MEMORY_BYTES = 2_000_000_000  # 2 GB
DEFAULT_CPU_SECONDS = 120
DEFAULT_FSIZE_BYTES = 200_000_000  # 200 MB

# 常驻父解释器的脚本（方案 §4.2 D 的「纯净父进程」）。
#
# ## 协议
#
# 外层：启动 → 等一行 `READY` → 循环「写一行 JSON 请求 / 读一行 JSON 结果」
#
#     请求 {"code": ..., "code_type": "python", "timeout_s": 10}
#     结果 {"exit_code": 0, "stdout": ..., "stderr": ..., "timed_out": false}
#
# 一行一帧：所以用户代码里的换行必须被 JSON 转义（`json.dumps` 会做）。
#
# ## 三条铁律（改这个脚本前必须读）
#
# 1. **父进程绝不执行用户代码** —— 只 import 依赖、fork、转发结果。
#    这是「既快又不泄漏」的唯一原因：父进程内存里不含任何用户数据，
#    所以 fork 给任何人的子进程都是干净的。
#
# 2. **子进程必须 `os._exit()`** —— 不能正常 return / sys.exit()。
#    正常退出会跑父进程上下文注册的 atexit / 第三方库 cleanup，
#    在子进程里跑会破坏父进程状态（清掉父进程持着的锁/临时文件）。
#
# 3. **预导入清单固定** —— 不能按请求动态 import。
#    动态 import 会把用户数据带进父进程 → 跨租户泄漏。
#
# ## 超时怎么处理（两级）
#
# 第一级：子进程自己 `signal.alarm(timeout)`。纯 Python 代码（死循环、
#        time.sleep、大部分库调用）会被 SIGALRM 中断 → 写超时结果 → daemon 存活。
# 第二级：外层 `asyncio.wait_for`。防的是 alarm 也拦不住的情况
#        （C 扩展里的死循环、alarm 被用户代码清掉）。这时只能杀掉**整个 daemon**，
#        下次 exec 重新预热——超时是异常路径，多付一次预热可接受。
_DAEMON_SCRIPT = r'''
import json, os, signal, sys, traceback

PRELOAD_ENV = "HLZ_PRELOAD"


class _ExecTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _ExecTimeout("execution exceeded")


def _child_run(req, wfd):
    """子进程：执行用户代码。**绝不 return**（铁律 2）。"""
    payload = {"exit_code": 1, "stdout": "", "stderr": "", "timed_out": False}
    try:
        timeout = float(req.get("timeout_s") or 120)
        code = req.get("code") or ""
        code_type = req.get("code_type") or "python"

        # umask 002：让这个子进程建的目录/文件**组可写**。
        #
        # ## 为什么必须在这里设
        #
        # 默认 022 下 `mkdir` 给 0755、`open()` 给 0644 —— **组不可写**。
        # 而 workspace 是 app（root）与沙箱服务（uid 1000）共享写的
        # （共享组 hlz，见 deploy/docker-compose.yml）。
        # 实测（2026-11-20）：code_run 里 `mkdir` 得到 `0o2755`、
        # `write_text` 得到 `0o100644`，两者组都不可写。
        #
        # 后果是一条只在特定序列里出现的坑：
        #
        #     code_run 写文件       → 成功（uid 1000 自己是属主，有 w）
        #     app 侧的 file_patch 改它 → 依赖目录/文件对组可写才能落下 tmp 文件
        #
        # ## 为什么是 002 而不是 022
        #
        # 002 = 「other 不可写、**组可写**」。与 `ensure_dir_shared` 的
        # `| 0o020` 是同一条不变量：workspace 里的一切都必须对共享组可写。
        # 只 reduce 组位、不动 other —— 不扩大对外暴露面。
        try:
            os.umask(0o002)
        except Exception:
            pass

        # 第一级超时：让 Python 层面的死循环/阻塞能被中断
        try:
            signal.signal(signal.SIGALRM, _alarm_handler)
            signal.alarm(max(1, int(timeout)))
        except Exception:
            pass

        if code_type == "sh":
            import subprocess
            r = subprocess.run(
                ["sh", "-c", code],
                capture_output=True, text=True, timeout=timeout,
            )
            payload = {"exit_code": r.returncode, "stdout": r.stdout,
                       "stderr": r.stderr, "timed_out": False}
        else:
            import contextlib, io
            out, err = io.StringIO(), io.StringIO()
            g = {"__name__": "__main__", "__builtins__": __builtins__}
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exec(compile(code, "<user_code>", "exec"), g)
            payload = {"exit_code": 0, "stdout": out.getvalue(),
                       "stderr": err.getvalue(), "timed_out": False}

    except _ExecTimeout:
        payload = {"exit_code": 124, "stdout": "",
                   "stderr": "[sandbox] execution exceeded time limit",
                   "timed_out": True}
    except SystemExit as e:
        code_v = e.code
        payload = {"exit_code": int(code_v) if isinstance(code_v, int) else 0,
                   "stdout": "", "stderr": "", "timed_out": False}
    except BaseException:
        payload = {"exit_code": 1, "stdout": "", "stderr": traceback.format_exc(),
                   "timed_out": False}
    finally:
        try:
            signal.alarm(0)
        except Exception:
            pass
        try:
            os.write(wfd, (json.dumps(payload) + "\n").encode())
            os.close(wfd)
        except Exception:
            pass
        os._exit(0)   # ← 铁律 2


def _preload():
    """只预热，永不执行用户代码（铁律 1 + 铁律 3）。"""
    for mod in (os.environ.get(PRELOAD_ENV) or "").split(","):
        mod = mod.strip()
        if not mod:
            continue
        try:
            __import__(mod)
            if mod == "matplotlib":
                import matplotlib
                matplotlib.use("Agg")
        except Exception:
            # 预导入失败不该让服务起不来（用户可以自己 import）
            pass


def _main():
    _preload()
    sys.stdout.write("READY\n")
    sys.stdout.flush()

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except Exception:
            continue

        rfd, wfd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(rfd)
            _child_run(req, wfd)      # 内部 os._exit，不会返回

        os.close(wfd)
        chunks = []
        while True:
            try:
                c = os.read(rfd, 65536)
            except OSError:
                break
            if not c:
                break
            chunks.append(c)
        os.close(rfd)
        try:
            os.waitpid(pid, 0)
        except Exception:
            pass

        sys.stdout.write(b"".join(chunks).decode("utf-8", "replace"))
        sys.stdout.flush()


_main()
'''


class BwrapExecutor:
    """用 bubblewrap 隔离执行。

    不需要 docker.sock、不需要容器运行时——bwrap 只调内核原语。
    """

    def __init__(
        self,
        *,
        workspace_resolver: Callable[[str], Optional[Path]],
        memory_bytes: int = DEFAULT_MEMORY_BYTES,
        cpu_seconds: int = DEFAULT_CPU_SECONDS,
        fsize_bytes: int = DEFAULT_FSIZE_BYTES,
        preload: str = "numpy,pandas,matplotlib",
        daemon_timeout_s: float = 60.0,
    ) -> None:
        self._resolve = workspace_resolver
        self._mem = memory_bytes
        self._cpu = cpu_seconds
        self._fsize = fsize_bytes
        self._preload = preload
        self._daemon_timeout_s = daemon_timeout_s
        # user_id -> Daemon（None 表示起不来，exec 时退回一次性模式）
        self._daemons: dict[str, Optional["_Daemon"]] = {}

    # ------------------------------------------------------------------ 构造

    def _base_argv(self, workspace: Path, *, cwd: str = "") -> list[str]:
        """bwrap 的公共部分（隔离参数 + 挂载 + prlimit 之前）。

        一次性模式和 daemon 模式共用，保证**两条路的隔离完全一致**——
        这很重要：如果 daemon 模式少一个 `--clearenv`，就会出现
        「走 daemon 的用户信息泄漏、走一次性模式的不泄漏」这种极难发现的差异。
        """
        argv: list[str] = [
            "bwrap",
            "--unshare-all",
            "--clearenv",
        ]
        for k, v in (
            ("PATH", "/usr/local/bin:/usr/bin:/bin"),
            ("HOME", "/tmp"),
            ("LANG", "C.UTF-8"),
            ("TZ", "Asia/Shanghai"),
            ("MPLCONFIGDIR", "/tmp/mpl"),
            ("PYTHONUNBUFFERED", "1"),
            ("HLZ_PRELOAD", self._preload),
        ):
            argv += ["--setenv", k, v]

        # --cap-drop ALL 放在 setenv 之后、挂载之前，顺序不影响语义，
        # 但测试按位置找参数，保持一致便于断言。
        argv += ["--cap-drop", "ALL"]

        for d in _RO_BINDS:
            argv += ["--ro-bind", d, d]

        # /etc 必须先用 tmpfs 占位，再叠需要的分支。
        # 不加 --tmpfs /etc 时，bwrap 为挂载点自动创建的 /etc 是**可写**的，
        # 于是 /etc/hosts 能凭空写进去（实测）。
        argv += ["--tmpfs", "/etc"]
        argv += ["--ro-bind", "/etc/fonts", "/etc/fonts"]

        # 只 bind 该用户自己的目录 —— 绝不 bind 父目录（跨租户隔离）
        argv += ["--bind", str(workspace), "/ws"]

        argv += ["--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev"]

        workdir = "/ws"
        if cwd:
            workdir = f"/ws/{cwd.strip('/')}"
        argv += ["--chdir", workdir]
        return argv

    def _build_argv(
        self,
        workspace: Path,
        code: str,
        code_type: str,
        *,
        cwd: str = "",
    ) -> list[str]:
        """一次性模式：bwrap python3 -c <code>。"""
        argv = self._base_argv(workspace, cwd=cwd)
        argv += [
            "prlimit",
            f"--as={self._mem}",
            f"--cpu={self._cpu}",
            f"--fsize={self._fsize}",
        ]
        # umask 002 必须显式设：默认 022 下 mkdir 给 0755、open 给 0644，
        # 组不可写 ⇒ 之后 app 侧（不同 uid）动这些文件就失败。
        # 与 daemon 模式的 `_child_run` 里那条是同一条不变量，
        # **两条执行路径必须一致**（否则「有时能改有时不能」极难排查）。
        #
        # 用 `sh -c 'umask 002; exec ...'` 而不是另写脚本文件：
        # 不改镜像、不引入新的挂载点，而且 `exec` 保证不留 sh 父进程
        # （signal / exit code 语义与直接跑一致）。
        if code_type == "sh":
            argv += ["sh", "-c", f"umask 002; {code}"]
        else:
            argv += ["sh", "-c", "umask 002; exec python3 -c \"$0\"", code]
        return argv

    def _build_daemon_argv(self, workspace: Path, *, cwd: str = "") -> list[str]:
        """daemon 模式：bwrap python3 -c <_DAEMON_SCRIPT>。

        ⚠️ 与一次性模式的唯一差别是「最后跑什么」。
        隔离参数、挂载、--setenv 全部走同一个 `_base_argv`，不可能漂移。

        ⚠️ **不加 prlimit --cpu**：daemon 是长活进程，累计 CPU 时间超过
        cpu_seconds 就会被 SIGKILL，那是把「给单次执行的上限」错用到常驻进程上。
        单个请求的 CPU 上限由子进程自己处理（超时靠 SIGALRM + 外层 wait_for）。
        `--as` / `--fsize` 仍然保留（它们是内存和文件大小，对 daemon 同样有意义）。
        """
        argv = self._base_argv(workspace, cwd=cwd)
        argv += [
            "prlimit",
            f"--as={self._mem}",
            f"--fsize={self._fsize}",
        ]
        argv += ["python3", "-c", _DAEMON_SCRIPT]
        return argv

    @staticmethod
    def _daemon_frame(code: str, code_type: str, timeout_s: float) -> str:
        """构造一行请求帧。

        ⚠️ 必须是**单行**：daemon 按行读，用户代码里的 `\\n` 由
        `json.dumps` 转义成 `\\\\n`，不会破坏帧边界。
        """
        return json.dumps(
            {"code": code, "code_type": code_type, "timeout_s": float(timeout_s)}
        ) + "\n"

    # ------------------------------------------------------------------ 生命周期

    async def start(self, user_id: str) -> bool:
        """起该用户的常驻解释器（预热），返回是否成功。

        ## 返回值而不是抛异常

        起不来（没装 bwrap / 资源不足 / 预热超时）**不该让整个服务崩**。
        返回 False，`exec` 会自动退回一次性模式——慢一点，但功能不丢。
        这个降级是可接受的：正确性不受影响，只影响延迟。

        ## 幂等

        已经有活着的 daemon 就直接返回 True，不重启。
        （重启会白白付一次预热成本。）
        """
        workspace = self._workspace_for(user_id)
        workspace.mkdir(parents=True, exist_ok=True)

        existing = self._daemons.get(user_id)
        if existing is not None and existing.alive:
            return True

        if shutil.which("bwrap") is None:
            self._daemons[user_id] = None
            return False

        argv = self._build_daemon_argv(workspace)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # ★ 必须独立会话（= 独立进程组）：bwrap 会 fork 一个内层进程，
                #   `proc.kill()` 只杀掉外层那个，内层被 init 收养后继续活着。
                #   实测：destroy 后仍剩 1 个 `stat=S` 的 bwrap，RSS ~1.3MB，
                #   且外层 kill 是 SIGKILL（不可捕获、无法转发给内层）。
                #   `start_new_session=True` 让整组在独立的进程组里，
                #   杀掉时用 `os.killpg` 一次带走整组（见 `_Daemon.kill`）。
                #   顺带好处：给 daemon Ctrl-C / SIGHUP 之类不会串到服务本身。
                start_new_session=True,
            )
        except OSError as exc:
            logger.warning("启动 daemon 失败 user=%s: %s", user_id, exc)
            self._daemons[user_id] = None
            return False

        daemon = _Daemon(user_id=user_id, proc=proc)
        # 等 READY（预热完成）。等不到就杀掉，退回一次性模式。
        try:
            ready = await asyncio.wait_for(
                daemon.wait_ready(), timeout=self._daemon_timeout_s
            )
        except asyncio.TimeoutError:
            logger.warning("daemon 预热超时（%ss）user=%s，退回一次性模式",
                           self._daemon_timeout_s, user_id)
            await daemon.kill()
            self._daemons[user_id] = None
            return False

        if not ready:
            # 预热阶段就死了（stderr 里会有原因）
            err = await daemon.read_stderr_brief()
            logger.warning("daemon 启动即退出 user=%s: %s", user_id, err)
            self._daemons[user_id] = None
            return False

        self._daemons[user_id] = daemon
        logger.info("daemon 就绪 user=%s pid=%s", user_id, proc.pid)
        return True

    async def destroy(self, user_id: str) -> None:
        """停掉该用户的 daemon。幂等。"""
        daemon = self._daemons.pop(user_id, None)
        if daemon is not None:
            await daemon.kill()
            logger.info("daemon 已停 user=%s", user_id)

    async def health(self) -> tuple[bool, str]:
        if shutil.which("bwrap") is None:
            return False, "bwrap 未安装（apt-get install bubblewrap）"
        n = sum(1 for d in self._daemons.values() if d is not None and d.alive)
        return True, f"ok（{n} 个 daemon 活跃）"

    # ------------------------------------------------------------------ 执行

    def _workspace_for(self, user_id: str) -> Path:
        ws = self._resolve(user_id)
        if ws is None:
            raise ValueError(
                f"无法解析用户 {user_id!r} 的 workspace："
                f"workspace_resolver 返回 None。"
                f"（不会退回临时目录——那会让用户 A 写到 B 那里或写到无处可查的地方）"
            )
        return Path(ws)

    async def exec(
        self,
        user_id: str,
        code: str,
        *,
        code_type: str = "python",
        timeout_s: float = 120.0,
        cwd: str = "",
    ) -> ExecResult:
        workspace = self._workspace_for(user_id)
        workspace.mkdir(parents=True, exist_ok=True)

        # 优先走 daemon（预热复用）；没有/死了就一次性
        daemon = self._daemons.get(user_id)
        if daemon is not None and daemon.alive:
            result = await self._exec_via_daemon(
                daemon, code, code_type, timeout_s, workspace
            )
            if result is not None:
                return result
            # daemon 在过程中死了 → 清掉，落到一次性模式
            self._daemons[user_id] = None
            logger.warning("daemon 中途失效 user=%s，本次退回一次性模式", user_id)

        return await self._exec_oneshot(workspace, code, code_type, timeout_s, cwd)

    async def _exec_via_daemon(
        self,
        daemon: "_Daemon",
        code: str,
        code_type: str,
        timeout_s: float,
        workspace: Path,
    ) -> Optional[ExecResult]:
        """通过 daemon 执行。返回 None 表示 daemon 已失效（调用方退回一次性）。

        ## 两级超时

        - daemon 内的子进程自己 `signal.alarm(timeout)`：**先于**外层触发。
          这样纯 Python 的死循环会被 SIGALRM 打断，daemon 存活。
        - 外层 `wait_for(timeout + margin)`：兜底。触发说明 alarm 没拦住
          （C 扩展死循环 / alarm 被清），只能杀掉整个 daemon。

        `margin` 给 10 秒：让 alarm 有机会先响（它要写结果、关管道、退出）。
        如果外层先超时，就把 daemon 杀掉——宁可下次重新预热，也不能卡住。
        """
        started = time.monotonic()
        frame = self._daemon_frame(code, code_type, timeout_s)
        margin = 10.0
        try:
            line = await asyncio.wait_for(
                daemon.roundtrip(frame), timeout=timeout_s + margin
            )
        except asyncio.TimeoutError:
            logger.warning("daemon 外层超时（%ss+%ss）user=%s，杀掉重启",
                           timeout_s, margin, daemon.user_id)
            await daemon.kill()
            return None
        except (BrokenPipeError, ConnectionResetError, asyncio.IncompleteReadError):
            return None

        if line is None:          # EOF：daemon 退出
            return None

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("daemon 返回非 JSON user=%s: %r", daemon.user_id, line[:200])
            return None

        duration_ms = int((time.monotonic() - started) * 1000)
        return ExecResult(
            exit_code=int(payload.get("exit_code", 1)),
            stdout=payload.get("stdout") or "",
            stderr=payload.get("stderr") or "",
            duration_ms=duration_ms,
            truncated=False,
        )

    async def _exec_oneshot(
        self,
        workspace: Path,
        code: str,
        code_type: str,
        timeout_s: float,
        cwd: str,
    ) -> ExecResult:
        """一次性执行：每次新起 bwrap python3 -c。"""
        argv = self._build_argv(workspace, code, code_type, cwd=cwd)

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            # 命令不存在 / 没执行权限（Windows 上没 bwrap、容器里漏装）
            # 必须转成结果而不是抛——一次环境问题不该让整个工具调用崩掉。
            return ExecResult(
                exit_code=127,
                stdout="",
                stderr=f"[sandbox] 无法启动 bwrap: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
                truncated=False,
            )

        timed_out = False
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            out_b, err_b = await proc.communicate()

        duration_ms = int((time.monotonic() - started) * 1000)

        stdout = (out_b or b"").decode("utf-8", errors="replace")
        stderr = (err_b or b"").decode("utf-8", errors="replace")
        if timed_out:
            stderr += f"\n[sandbox] execution exceeded {timeout_s}s and was killed"

        return ExecResult(
            exit_code=124 if timed_out else (proc.returncode or 0),
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            truncated=False,
        )


class _Daemon:
    """一个常驻父解释器的句柄。

    ## 为什么需要「串行化」

    daemon 的协议是「写一行、读一行」。如果两个请求同时跑，
    第二个的请求行会插到第一个的结果行之前 → 结果与请求错位
    （A 拿到 B 的输出）。这是最危险的一类 bug：**不报错、只出错数据**。

    所以用一把 `asyncio.Lock` 串行化。代价是同一用户的并发执行会排队——
    这本来也是合理的：一个 slot 服务一个用户，同一用户同时跑多段代码
    本来就该串行（共享同一个 workspace）。
    """

    def __init__(self, *, user_id: str, proc: asyncio.subprocess.Process) -> None:
        self.user_id = user_id
        self.proc = proc
        self._lock = asyncio.Lock()
        self._ready_seen = False

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    async def wait_ready(self) -> bool:
        """读第一行，期望 `READY`。EOF（进程退出）返回 False。"""
        if self._ready_seen:
            return True
        assert self.proc.stdout is not None
        line = await self.proc.stdout.readline()
        if not line:
            return False
        if line.strip() == b"READY":
            self._ready_seen = True
            return True
        # 不是 READY：可能预热脚本把别的东西打到了 stdout。
        # 不当成致命错误——只要进程还活着就继续（后续帧仍能对齐）。
        logger.warning("daemon 首行不是 READY user=%s: %r", self.user_id, line[:100])
        return self.alive

    async def roundtrip(self, frame: str) -> Optional[str]:
        """发一帧、收一帧。EOF 返回 None。"""
        async with self._lock:
            if not self.alive:
                return None
            assert self.proc.stdin is not None and self.proc.stdout is not None
            try:
                self.proc.stdin.write(frame.encode())
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                return None
            line = await self.proc.stdout.readline()
            if not line:
                return None
            return line.decode("utf-8", "replace")

    async def read_stderr_brief(self) -> str:
        """读一小段 stderr（用于日志），不阻塞太久。"""
        if self.proc.stderr is None:
            return ""
        try:
            data = await asyncio.wait_for(self.proc.stderr.read(2000), timeout=1.0)
            return data.decode("utf-8", "replace")[:500]
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return ""

    async def kill(self) -> None:
        """停掉整个 daemon **进程组**。

        ## 为什么不能只 `proc.kill()`

        bwrap 会 fork 一个内层进程（实测 `ps` 能看到两条同 workspace 的 bwrap，
        ppid 链是 386 ← 387）。`proc.kill()` 只对最外层那个发 SIGKILL，
        而 SIGKILL 不可捕获 —— 外层死了就没机会把信号转发给内层，
        内层随即被 init 收养，继续跑。

        实测症状：日志照常打「daemon 已停」，`ps` 里却留着 `stat=S` 的 bwrap，
        每条泄漏 RSS 约 1.3MB 且压着 `prlimit --as=2GB`。这个量级单看很小，
        但槽是按空闲 TTL 反复建/销的，累积几天就是容器 OOM。
        **靠日志永远看不出来，只能数进程**（见 `work/probe_daemon_after_destroy.py`）。

        ## 为什么用 killpg 而不是遍历子进程

        以 daemon 为根的整棵树都在它自己的进程组里（`start_new_session=True`
        让 bwrap 成为组长），`killpg` 一条命令覆盖全部成员，不必递归找子进程。
        """
        if self.proc.returncode is not None:
            return
        pgid = self.proc.pid          # start_new_session=True ⇒ pid == pgid
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            # 组已经没了 / 不在同一用户下：退回单 pid kill
            try:
                self.proc.kill()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("daemon 杀不掉 user=%s pid=%s", self.user_id, self.proc.pid)