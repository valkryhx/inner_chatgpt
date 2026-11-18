"""启动自检：Docker 可达？镜像存在？残留容器？

设计文档 §1/§9.1：**不可用时返回原因，不抛异常** —— 让调用方（policy/manager）
决定是失败还是降级。这里只负责回答「能不能用」。
"""
from __future__ import annotations

import logging

from .config import SandboxConfig, get_config
from .errors import SandboxUnavailable

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


def cleanup_orphans() -> int:
    """删除上次进程遗留的沙箱容器。返回删除数量。

    只删带 ``hlz.sandbox=1`` 标签的容器 —— 这是本方案独占的命名空间。
    ⚠️ 多 worker 部署时会误删其它 worker 的容器（设计文档 §10.5），
    届时需给标签加 worker_id；MVP 单 worker，安全。
    """
    try:
        client = _client()
    except SandboxUnavailable as exc:
        logger.debug("cleanup_orphans 跳过：%s", exc.reason)
        return 0
    except Exception as exc:  # noqa: BLE001 - 契约是「清理失败绝不阻断启动」
        logger.warning("cleanup_orphans 无法构造 docker 客户端：%s", exc)
        return 0
    removed = 0
    try:
        containers = client.containers.list(
            all=True, filters={"label": f"{SANDBOX_LABEL}={SANDBOX_LABEL_VALUE}"}
        )
        for c in containers:
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