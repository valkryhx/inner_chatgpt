"""启动自检：Docker 可达？镜像存在？残留容器？

设计文档 §1/§9.1：**不可用时返回原因，不抛异常** —— 让调用方（policy/manager）
决定是失败还是降级。这里只负责回答「能不能用」。
"""
from __future__ import annotations

import logging

from .config import SandboxConfig, get_config
from .errors import SandboxUnavailable
from .identity import OWNER_LABEL, owner_id, owner_machine

logger = logging.getLogger(__name__)

# 本方案独占的容器标签（设计文档 §10.5：按 label 而非名字前缀清扫，避免撞名）
SANDBOX_LABEL = "hlz.sandbox"
SANDBOX_LABEL_VALUE = "1"


def _client():
    """构造 docker 客户端。导入失败/连接失败都抛 SandboxUnavailable。"""
    try:
        import docker  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise SandboxUnavailable(f"未安装 docker SDK（{exc.__class__.__name__}）") from exc
    try:
        client = docker.from_env()
    except Exception as exc:  # noqa: BLE001
        raise SandboxUnavailable(f"无法连接 Docker（{exc.__class__.__name__}）") from exc
    return client


def docker_ready() -> tuple[bool, str]:
    """Docker 是否可用。返回 ``(ok, reason)``，不抛异常。"""
    try:
        client = _client()
    except SandboxUnavailable as exc:
        return False, exc.reason
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        return False, f"Docker 未运行或不可达（{exc.__class__.__name__}）"
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return True, ""


def image_exists(image: str | None = None) -> tuple[bool, str]:
    """镜像是否已在本地。不自动 pull（拉取受网络环境影响，且耗时不可控）。"""
    cfg = get_config()
    tag = image or cfg.image
    try:
        client = _client()
    except SandboxUnavailable as exc:
        return False, exc.reason
    try:
        client.images.get(tag)
    except Exception:  # noqa: BLE001
        return False, f"镜像不存在：{tag}（请先 docker pull）"
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return True, ""


def check(cfg: SandboxConfig | None = None) -> tuple[bool, str]:
    """完整自检。``mode=off`` 时直接返回可用（不碰 Docker）。"""
    cfg = cfg or get_config()
    if not cfg.enabled:
        return True, ""

    ok, reason = docker_ready()
    if not ok:
        return False, reason
    ok, reason = image_exists(cfg.image)
    if not ok:
        return False, reason
    return True, ""


def cleanup_orphans(*, all_owners: bool = False, same_machine: bool = False) -> int:
    """删除**属于本进程**的残留沙箱容器。返回删除数量。

    ## 为什么默认只清自己的（2026-11-18 改）

    原实现只按 ``hlz.sandbox=1`` 过滤，**把全机的沙箱容器都删了**。
    单进程时这没错（那些容器确实是自己上一轮留下的）；但一旦扩容：

    - 同机跑两个后端进程 -> 新进程启动时会把**另一个进程正在用**的容器删掉；
    - 多机共用一个 Docker daemon -> 会跨机误删。

    现在按 ``hlz.owner`` 标签过滤，默认只清 ``<hostname>:<pid>`` 自己的。
    注意：本进程刚启动时 pid 是新的，所以严格说清不到"上一次自己"——
    上一轮的容器 owner 是旧 pid，属于"别人的"。这类残留由：

    1. ``same_machine=True``（清本机所有）手动处理；
    2. TTL 回收协程（`pool.reap_idle`）自然清掉；
    3. 运维显式 ``all_owners=True``。

    这里选择的默认值偏保守：**宁可留残留，也不能删掉正在干活的容器**。

    ## 参数

    - ``all_owners=True``：清全部沙箱容器（运维手动清场用）；
    - ``same_machine=True``：只按主机名过滤，清本机所有实例的（介于两者之间）；
    - 都不传：只清本进程自己的（最安全）。
    """
    try:
        client = _client()
    except SandboxUnavailable as exc:
        logger.debug("cleanup_orphans 跳过：%s", exc.reason)
        return 0
    except Exception as exc:  # noqa: BLE001 - 契约是「清理失败绝不阻断启动」
        logger.warning("cleanup_orphans 无法构造 docker 客户端：%s", exc)
        return 0

    # 构造 label 过滤：始终要求是本方案的沙箱容器，再按归属收窄。
    label_filters = [f"{SANDBOX_LABEL}={SANDBOX_LABEL_VALUE}"]
    if not all_owners:
        if same_machine:
            # docker 标签过滤不支持前缀匹配，只能在取回后自己筛。
            pass
        else:
            label_filters.append(f"{OWNER_LABEL}={owner_id()}")

    removed = 0
    try:
        containers = client.containers.list(all=True, filters={"label": label_filters})
        for c in containers:
            if same_machine and not all_owners:
                got = (getattr(c, "labels", None) or {}).get(OWNER_LABEL, "")
                if not got.startswith(f"{owner_machine()}:"):
                    continue
            try:
                c.remove(force=True)
                removed += 1
            except Exception:  # noqa: BLE001
                logger.warning("清理沙箱容器失败：%s", getattr(c, "name", "?"), exc_info=True)
    except Exception:  # noqa: BLE001
        logger.warning("cleanup_orphans 列举容器失败", exc_info=True)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    if removed:
        logger.info("已清理 %d 个残留沙箱容器", removed)
    return removed