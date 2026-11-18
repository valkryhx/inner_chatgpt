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

logger = logging.getLogger(__name__)

CONTAINER_PREFIX = "hlz_sbx_"
WORKSPACE_MOUNT = "/ws"


def container_name(user_id: str) -> str:
    """容器名。用户 id 形如 ``u_c56b8337dd09``，本身已是安全字符集。"""
    safe = "".join(ch for ch in user_id if ch.isalnum() or ch in "_-")
    return f"{CONTAINER_PREFIX}{safe}"


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
                labels={SANDBOX_LABEL: SANDBOX_LABEL_VALUE},
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
        host_workspace: str = "",
    ) -> ExecResult:
        """在容器内执行一段 Python 代码。

        实现选型（2026-11-18 四次实测后定稿，设计文档 §10.1）：

        | 方案 | 结果 |
        | --- | --- |
        | ``exec_run(stdin=True)`` 写 stdin | ❌ 返回的 socket 是输出通道，写入后读不到输出 |
        | 低层 ``exec_start(socket=True)`` | ❌ 本机 Docker Desktop + Windows 上拿不到可写句柄，退出码 -1 |
        | ``put_archive`` 写容器内 ``/tmp`` | ❌ **tmpfs 上的 archive 解包失效**：返回 True 但 `ls` 为空 |
        | **写宿主 workspace 下的临时文件（bind mount 双向可见）** | ✅ 稳定 |

        临时目录放在 workspace 的 ``.hlz_tmp/`` 下并**执行完立即删除**。
        这会在用户的 ``list_files`` 里短暂出现，但 ``.`` 前缀已被项目惯例用作隐藏目录
        （``.subagents`` / ``.tmp``），且清理在 finally 里，正常路径不留痕。
        """
        import os
        import tempfile

        if not host_workspace:
            raise SandboxExecError("exec_python 需要 host_workspace（宿主 workspace 路径）")

        marker = uuid.uuid4().hex
        host_dir = os.path.join(host_workspace, ".hlz_tmp")
        os.makedirs(host_dir, exist_ok=True)
        host_path = os.path.join(host_dir, f"_hlz_{marker}.py")
        remote = f"{WORKSPACE_MOUNT}/.hlz_tmp/_hlz_{marker}.py"
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

        ⚠️ ``python:3.12-slim`` 基于 Debian，有 ``bash`` 与 ``sh``；
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
        """按 marker 精确杀进程，避免误杀同容器里其它会话的进程。"""
        client = None
        try:
            client = _client()
            container = client.containers.get(container_id)
            container.exec_run(
                ["sh", "-c", f"pkill -f 'HLZ_EXEC_MARKER={marker}' 2>/dev/null || true"],
                demux=True,
            )
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