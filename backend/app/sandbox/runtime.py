"""单容器生命周期：create / exec / stop / destroy。

设计文档 §6.3（容器规格）、§6.5（停止与回收）、§10.2（输出解码）。

**本模块只管「一个容器」，不管「有几个容器」** —— 后者是 ``pool`` 的事。
这个划分来自 ``20261111`` 方案 §3.7，好处是改 TTL 逻辑不必动 exec 代码。

路径映射（设计文档 §6.3）：
- 宿主 ``backend/data/workspaces/<user_id>/``  ->  容器内 ``/ws``
- 容器内 ``/tmp`` 是 tmpfs，容器销毁即消失
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from .config import SandboxConfig, get_config
from .errors import SandboxExecError, SandboxTimeout, SandboxUnavailable
from .probe import SANDBOX_LABEL, SANDBOX_LABEL_VALUE
from .identity import OWNER_LABEL, owner_id as _self_owner

logger = logging.getLogger(__name__)

CONTAINER_PREFIX = "hlz_sbx_"
WORKSPACE_MOUNT = "/ws"


def container_name(user_id: str, *, owner: str | None = None) -> str:
    """容器名：``hlz_sbx_<user>_<owner>``。

    **为什么要带 owner**（2026-11-18 扩容前的准备）：

    - 原实现是 ``hlz_sbx_<user_id>``，单进程没问题；
    - 一旦同机跑两个后端进程（或多机共用一个 Docker daemon），
      两个进程为同一用户建容器时会撞名 —— Docker 直接拒绝：
      ``Conflict. The container name "/hlz_sbx_u_x" is already in use``；
    - 更糟的是 ``_remove_by_name`` 会先删同名再建，于是**进程 A 会把进程 B
      正在用的容器删掉**，表现为「任务跑到一半容器没了」。

    带 owner 后，不同实例天然隔离，谁也不会碰谁的容器。

    **为了人工排查方便**，user_id 保留在名字里，且 ``docker ps`` 一眼能看出
    归属。owner 里含主机名，多机时也能区分。

    ``owner`` 省略时取本进程的 owner_id —— 保持调用方简洁。
    """
    from .identity import _SAFE, owner_id as _owner_id

    safe_user = "".join(ch for ch in user_id if ch.isalnum() or ch in "_-")
    if owner is None:
        owner = _owner_id()
    safe_owner = _SAFE.sub("-", owner)[:64]
    return f"{CONTAINER_PREFIX}{safe_user}_{safe_owner}"


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    marker: str


def _client():
    try:
        import docker  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise SandboxUnavailable(f"未安装 docker SDK（{exc.__class__.__name__}）") from exc
    try:
        return docker.from_env()
    except Exception as exc:  # noqa: BLE001
        raise SandboxUnavailable(f"无法连接 Docker（{exc.__class__.__name__}）") from exc


class ContainerRuntime:
    """一个用户容器的操作集合。所有方法都是「尽力而为 + 明确报错」。"""

    def __init__(self, cfg: SandboxConfig | None = None) -> None:
        self.cfg = cfg or get_config()

    def _exec_quiet_sync(self, container_id: str, argv: list[str]) -> None:
        """执行一条命令但忽略一切结果（清理用：rm 临时文件）。"""
        client = None
        try:
            client = _client()
            exec_id = client.api.exec_create(container_id, argv, stdin=False, tty=False)["Id"]
            client.api.exec_start(exec_id, socket=False, tty=False)
        except Exception:  # noqa: BLE001
            logger.debug("清理命令失败（忽略）：%s", argv, exc_info=True)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---------------- 创建 ----------------

    def create(self, user_id: str, host_workspace: str) -> str:
        """创建并启动常驻容器，返回 container id。

        ``host_workspace`` 必须是宿主上**该用户自己的** workspace 绝对路径。
        只挂这一个目录 —— 这是隔离的物理基础（设计文档 §6.3）。
        """
        client = _client()
        name = container_name(user_id)
        try:
            # 同名残留先清掉（上一次进程崩溃可能留下）
            self._remove_by_name(client, name)

            container = client.containers.run(
                self.cfg.image,
                ["sleep", "infinity"],
                name=name,
                detach=True,
                labels={
                    SANDBOX_LABEL: SANDBOX_LABEL_VALUE,
                    # 归属标签：清扫时靠它区分「我的」和「别人的」。
                    # 没有这个标签，新进程启动会把其它进程正在用的容器删掉。
                    OWNER_LABEL: _self_owner(),
                },
                # —— 隔离（设计文档 §6.3）——
                network_mode="none",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                # —— 资源 ——
                mem_limit=self.cfg.memory,
                memswap_limit=self.cfg.memory,
                nano_cpus=int(float(self.cfg.cpus) * 1_000_000_000),
                pids_limit=self.cfg.pids_limit,
                # —— 文件系统：只挂该用户 workspace ——
                volumes={host_workspace: {"bind": WORKSPACE_MOUNT, "mode": "rw"}},
                tmpfs={"/tmp": f"size={self.cfg.tmpfs_size},mode=1777"},
                # —— 环境变量必须显式给全（Docker Desktop 会继承宿主 HOME）——
                environment={
                    "HOME": "/tmp",
                    "TMPDIR": "/tmp",
                    "XDG_CACHE_HOME": "/tmp/.cache",
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                working_dir=WORKSPACE_MOUNT,
            )
            logger.info("沙箱容器已创建 user=%s id=%s", user_id, container.short_id)
            return container.id
        except SandboxUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SandboxExecError(f"创建沙箱容器失败：{exc}") from exc
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _remove_by_name(client, name: str) -> None:
        try:
            old = client.containers.get(name)
        except Exception:  # noqa: BLE001 - 不存在就是好事
            return
        try:
            old.remove(force=True)
            logger.info("清理同名残留容器 %s", name)
        except Exception:  # noqa: BLE001
            logger.warning("清理同名残留容器失败 %s", name, exc_info=True)

    # ---------------- 执行 ----------------

    async def exec_python(
        self,
        container_id: str,
        code: str,
        *,
        timeout_s: float,
        cwd: str = WORKSPACE_MOUNT,
        host_workspace: str,
        container_workspace: str = "",
    ) -> ExecResult:
        """在容器内执行一段 Python 代码。

        实现选型（2026-11-18 四次实测后定稿，设计文档 §10.1）：

        | 方案 | 结果 |
        | --- | --- |
        | ``exec_run(stdin=True)`` 写 stdin | ❌ 返回的 socket 是输出通道，写入后读不到输出 |
        | 低层 ``exec_start(socket=True)`` | ❌ 本机 Docker Desktop + Windows 上拿不到可写句柄，退出码 -1 |
        | ``put_archive`` 写容器内 ``/tmp`` | ❌ **tmpfs 上的 archive 解包失效**：返回 True 但 `ls` 为空 |
        | **写宿主 workspace 下的临时文件（bind mount 双向可见）** | ✅ 稳定 |

        **两个 host 路径参数的区别（多智能体的正确性关键）**：

        - ``host_workspace``: 脚本**真正写到**的宿主目录（含子代理的 write_root）。
          如 ``D:\\sandbox\\u_a\\.subagents\\sess\\a1``
        - ``container_workspace``: 容器 ``/ws`` **挂载的**宿主根目录。
          如 ``D:\\sandbox\\u_a``

        容器只挂根目录（见 ``manager._mount_root`` 的说明），所以脚本的
        容器内路径必须由「相对根目录的子路径」算出来，不能直接取
        ``WORKSPACE_MOUNT + 文件名``。

        ``container_workspace`` 为空时退化为「把 host_workspace 当挂载根」，
        保持向后兼容（单 agent 场景下两者相同）。
        """
        import os

        if not host_workspace:
            raise SandboxExecError("exec_python 需要 host_workspace（宿主 workspace 路径）")

        mount_root = container_workspace or host_workspace

        marker = uuid.uuid4().hex
        host_dir = os.path.join(host_workspace, ".hlz_tmp")
        os.makedirs(host_dir, exist_ok=True)
        host_path = os.path.join(host_dir, f"_hlz_{marker}.py")

        # 容器内路径 = /ws + (脚本相对挂载根的路径)
        try:
            rel = os.path.relpath(host_path, mount_root).replace(os.sep, "/")
        except ValueError:
            # Windows 上跨盘符会 ValueError —— 说明挂载根与脚本位置不在同一棵树
            raise SandboxExecError(
                f"脚本路径 {host_path} 不在挂载根 {mount_root} 之下，"
                "容器内无法访问（检查 write_root 与容器挂载点是否一致）"
            ) from None
        if rel.startswith("../") or rel == "..":
            raise SandboxExecError(
                f"脚本路径 {host_path} 不在挂载根 {mount_root} 之下，"
                "容器内无法访问（检查 write_root 与容器挂载点是否一致）"
            )
        remote = f"{WORKSPACE_MOUNT}/{rel}"

        with open(host_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(code)
        try:
            return await self._exec(container_id, ["python", "-u", remote],
                                    timeout_s=timeout_s, marker=marker, cwd=cwd)
        finally:
            try:
                os.remove(host_path)
            except OSError:
                pass

    async def exec_shell(
        self,
        container_id: str,
        code: str,
        *,
        timeout_s: float,
        cwd: str = WORKSPACE_MOUNT,
    ) -> ExecResult:
        """在容器内执行一段 shell 代码（``sh -c``）。

        ``cwd`` 决定工作目录。多智能体场景下会传
        ``/ws/.subagents/<session>/<agent>``，让子代理的相对路径落在自己目录里。

        ⚠️ ``hlz-sandbox`` 基于 Debian，有 ``bash`` 与 ``sh``；
        若换 Alpine 镜像则只有 ``sh``（设计文档 §10.1）。
        """
        marker = uuid.uuid4().hex
        argv = ["sh", "-c", code]
        return await self._exec(container_id, argv, timeout_s=timeout_s,
                                marker=marker, cwd=cwd)

    async def _exec(
        self,
        container_id: str,
        argv: list[str],
        *,
        timeout_s: float,
        marker: str,
        cwd: str,
    ) -> ExecResult:
        """实际执行。同步 docker SDK 调用放进线程池，避免阻塞事件循环。"""

        def _run() -> tuple[int, bytes]:
            client = _client()
            try:
                container = client.containers.get(container_id)
                # exec_run + demux=True：output 是 (stdout, stderr) 二元组。
                # 合并成一路，与进程内路径 `stderr=STDOUT` 的语义一致（设计文档 §10.2）。
                result = container.exec_run(
                    argv,
                    demux=True,
                    workdir=cwd,
                    environment={"HLZ_EXEC_MARKER": marker},
                )
                out, err = (result.output or (None, None)) if hasattr(result, "output") else (None, None)
                raw = (out or b"") + (err or b"")
                return int(result.exit_code if result.exit_code is not None else -1), raw
            finally:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            exit_code, raw = await asyncio.wait_for(
                asyncio.to_thread(_run), timeout=timeout_s
            )
        except (asyncio.TimeoutError, TimeoutError):
            await asyncio.to_thread(self._pkill_marker_sync, container_id, marker)
            raise SandboxTimeout(timeout_s) from None
        except SandboxUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SandboxExecError(f"容器内执行失败：{exc}") from exc

        # 与进程内路径完全一致的解码方式（设计文档 §10.2）
        return ExecResult(exit_code=exit_code, stdout=raw.decode("utf-8", errors="replace"),
                          marker=marker)

    def _put_text_sync(self, container_id: str, remote_path: str, text: str) -> None:
        """把一段文本写到容器的某个路径（用 tar 流，不依赖 stdin）。

        ``put_archive`` 需要的是「目标目录 + 一个 tar 流」，
        所以这里把文本打成单文件 tar，写到目标目录。
        """
        import io
        import os.path
        import tarfile

        target_dir = os.path.dirname(remote_path)
        name = os.path.basename(remote_path)
        data = text.encode("utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
        client = None
        try:
            client = _client()
            container = client.containers.get(container_id)
            ok = container.put_archive(target_dir, buf.getvalue())
            if not ok:
                raise SandboxExecError(f"写入容器临时脚本失败：{remote_path}")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---------------- 停止 ----------------

    def _pkill_marker_sync(self, container_id: str, marker: str) -> None:
        """按 marker 精确杀进程，避免误杀同容器里其它会话的进程。

        与 ``exec_shell_sync_killall`` 同理：不依赖 pkill（见那个方法的说明），
        改用遍历 ``/proc`` 匹配环境变量。读 ``/proc/<pid>/environ`` 找
        ``HLZ_EXEC_MARKER=<marker>`` —— ``_exec`` 通过 ``environment=``
        把它注入了每个执行进程。

        这是 ``_exec`` 超时分支调的：超时后必须让那个进程真的死掉，
        否则调用方已经放弃等待、进程还在烧 CPU，比不超时更糟。
        """
        needle = f"HLZ_EXEC_MARKER={marker}".encode()
        script = (
            "import os, signal, sys\n"
            f"needle = {needle!r}\n"
            "killed = 0\n"
            "me = os.getpid()\n"
            "for pid in os.listdir('/proc'):\n"
            "    if not pid.isdigit() or int(pid) == me:\n"
            "        continue\n"
            "    try:\n"
            "        with open('/proc/' + pid + '/environ', 'rb') as fh:\n"
            "            env = fh.read()\n"
            "    except OSError:\n"
            "        continue\n"
            "    if needle not in env:\n"
            "        continue\n"
            "    try:\n"
            "        os.kill(int(pid), signal.SIGKILL)\n"
            "        killed += 1\n"
            "    except OSError:\n"
            "        pass\n"
            "print(killed, flush=True)\n"
        )
        client = None
        try:
            client = _client()
            container = client.containers.get(container_id)
            container.exec_run(["python", "-c", script], demux=True)
        except Exception:  # noqa: BLE001
            logger.debug("pkill marker 失败（容器可能已消失）", exc_info=True)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    async def stop_processes(self, container_id: str, marker: str) -> None:
        await asyncio.to_thread(self._pkill_marker_sync, container_id, marker)

    def exec_shell_sync_killall(self, container_id: str) -> int:
        """杀掉容器内所有 python 子进程（/stop 级联用）。返回杀掉的进程数。

        ⚠️ **不用 pkill / ps，走 /proc**。两层原因：

        1. **最初的事故**：第一版用 ``pkill -f``，而当时的基础镜像
           ``python:3.12-slim`` 是 Debian slim、**没有 procps 包**
           （``pkill`` / ``ps`` / ``pgrep`` 全不存在，``which`` 全空）。
           ``pkill -f`` 静默失败、返回 0 —— `/stop` 看起来成功，
           容器内进程照样跑完，是彻底失效的假信号（§8.5「没报错 ≠ 有效信号」）。

        2. **即使现在有了 procps 也不改回去**（默认镜像已装 procps）：
           - 依赖外部命令 = 依赖镜像内容。换镜像、裁剪镜像就静默失效，
             而且失效时**没有报错**，只会表现为「停止不生效」；
           - ``pkill -f`` 是子串匹配，容易误杀；
           - ``/proc`` 是内核提供的，任何 Linux 镜像上都有，零依赖。

        实现：遍历 ``/proc/<pid>/cmdline``，任一 argv **以 ``/ws/`` 开头**
        就 SIGKILL。为什么按前缀而不是子串：``python -c <内联源码>`` 的
        argv 里可能含 ``/ws/`` 字样（第一版因此把查询脚本自己也算成业务进程，
        计数从 7 降到 1 而非 0）。跳过自己那个 ``python -c`` 进程。

        粗粒度：同用户多会话共享一个容器，这里会把该用户其它会话的执行也杀掉。
        ``20261111`` 方案 §2.3 已承认「同用户共享容器」是已知取舍。
        需要精确到单次执行时用 ``_pkill_marker_sync(marker)``。
        """
        script = (
            "import os, signal\n"
            "killed = 0\n"
            "me = os.getpid()\n"
            "for pid in os.listdir('/proc'):\n"
            "    if not pid.isdigit() or int(pid) == me:\n"
            "        continue\n"
            "    try:\n"
            "        parts = open('/proc/' + pid + '/cmdline', 'rb').read().split(b'\\x00')\n"
            "    except OSError:\n"
            "        continue\n"
            "    # 任一 argv **以 /ws/ 开头**才算业务进程。\n"
            "    # 为什么不是 parts[1]：真实调用是 `python -u /ws/x.py`，脚本在 argv[2]。\n"
            "    # 为什么不会误杀查询脚本：`python -c <内联源码>` 的 argv[2] 以 'import' 开头，\n"
            "    # 源码正文里虽然含 '/ws/' 但不以它开头。\n"
            "    hit = any(p.startswith(b'/ws/') for p in parts)\n"
            "    if not hit:\n"
            "        hit = b'HLZ_EXEC_MARKER' in b' '.join(parts)\n"
            "    if not hit:\n"
            "        continue\n"
            "    try:\n"
            "        os.kill(int(pid), signal.SIGKILL)\n"
            "        killed += 1\n"
            "    except OSError:\n"
            "        pass\n"
            "print(killed, flush=True)\n"
        )
        client = None
        try:
            client = _client()
            container = client.containers.get(container_id)
            res = container.exec_run(["python", "-c", script], demux=True)
            out = (res.output[0] or b"") if hasattr(res, "output") else b""
            text = (out or b"").decode("utf-8", errors="replace").strip()
            return int(text) if text.isdigit() else 0
        except Exception:  # noqa: BLE001
            logger.debug("killall 失败（容器可能已消失）", exc_info=True)
            return 0
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---------------- 销毁 ----------------

    def destroy(self, container_id: str) -> None:
        """强制删除容器。幂等：容器已不存在不报错。"""
        client = None
        try:
            client = _client()
            container = client.containers.get(container_id)
            container.remove(force=True)
            logger.info("沙箱容器已销毁 %s", container_id[:12])
        except SandboxUnavailable:
            raise
        except Exception:  # noqa: BLE001 - 已不存在视为成功
            logger.debug("销毁容器失败或已不存在 %s", container_id[:12], exc_info=True)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    def is_alive(self, container_id: str) -> bool:
        client = None
        try:
            client = _client()
            container = client.containers.get(container_id)
            return container.status == "running"
        except Exception:  # noqa: BLE001
            return False
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass