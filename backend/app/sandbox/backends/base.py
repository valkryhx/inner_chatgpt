"""后端抽象：app 侧用哪条路径执行代码。

## 与 `sandbox_service.executor.Executor` 的区别

两者长得像，但**不是同一层**，不要合并：

| | `Executor`（服务内） | `Backend`（app 内） |
| --- | --- | --- |
| 职责 | 真的把代码跑起来 | 决定**走哪条路**去跑 |
| 返回 | `ExecResult` | `(handled, output, marker)` |
| 是否关心降级 | 不关心（它只负责执行） | **关心**（auto 降级 / strict 报错） |
| 数量 | 3 个（bwrap / gvisor / inproc） | 3 个（service / inproc / off） |

`handled` 这个三元组是 app 侧既有的接口约定（`manager.exec_code` 的签名），
阶段 2 刻意**保持不变**——这样 `builtin_tools.code_run` 一行都不用改（方案 §9 阶段 2）。

## 为什么要 `off` 这个后端

`off` 不是一个「执行实现」，而是「不执行」。把它做成后端而不是在调用方加 if，
是为了让「off 时一个 HTTP 都不发」这条断言有唯一的落点（方案 §6.4）。
"""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

VALID_BACKENDS = ("off", "service", "inproc")


@runtime_checkable
class Backend(Protocol):
    """app 侧的执行后端。"""

    name: str

    async def health(self) -> tuple[bool, str]:
        """这个后端**自己**现在能不能干活。返回 ``(ok, reason)``。

        ## 为什么必须由后端自己回答

        原来可用性判断走 `probe.check(cfg)`，而 `probe.py` 里全是
        `docker.from_env()`。当前部署是 `backend=service`（走 HTTP 到沙箱服务），
        根本不会用 Docker —— 探针检查的是一个**不会被使用的东西**。

        后果是两头都错（都实测过）：

        - 容器里没装 docker SDK → 永远判「不可用」→ 每次都误降级
        - 本机装了 docker SDK → 永远判「可用」→ 沙箱真挂了也当没事

        所以判断必须问**当前后端**。`bwrap` 的 daemon 活着不代表 service
        可达；service 可达也不代表 Docker 镜像在。两者不可互相代表。

        ## 实现约定

        - **不抛异常**：不可用是返回值，不是异常。调用方（manager）按 mode
          决定是降级还是失败。
        - `off` 恒返回 True：它什么都不做，没有「不可用」这种状态。
        """
        ...

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
        """返回 (handled, output, marker)。"""
        ...


def _make_executor():
    """构造远程客户端的底层执行器（测试可替换）。"""
    raise NotImplementedError


def get_backend(*, reload: bool = False) -> Backend:
    """按 mode + backend 选实现。

    ⚠️ **不做模块级缓存**。
    最初用 `_cached` 缓存结果，结果测试之间互相污染：
    `test_sandbox_backend_selection.py` 里 `monkeypatch.setenv("MODE","off")`
    之后拿到的 OffBackend 被后续测试复用，导致 `test_sandbox_multiagent`
    等既有测试在**整体跑**时失败、单独跑时通过——典型的隐式全局状态。

    不缓存每次重读环境变量的代价是一次 `cfg.mode` 比较 + 一次 dict 取值，
    相对一次容器/HTTP 调用可以忽略。**正确性优先于这点开销。**

    选择顺序：
        1. mode == "off"          → OffBackend（一个 HTTP 都不发）
        2. backend == "inproc"    → InprocBackend（现有 Docker，回滚路径）
        3. backend == "service"   → ServiceBackend（调沙箱服务）
        4. 其它                   → **报错**
    """
    from .. import config as sbx_config

    # reload 参数保留以兼容调用方；现在总是现读，所以只有语义意义
    cfg = sbx_config.get_config(reload=reload)

    if cfg.mode == "off":
        from .off import OffBackend

        return OffBackend()

    kind = (os.environ.get("HLZ_SANDBOX_BACKEND") or "inproc").strip().lower()

    if kind == "inproc":
        from .inproc import InprocBackend

        return InprocBackend()
    if kind == "service":
        from .service import ServiceBackend

        return ServiceBackend()

    raise ValueError(
        f"unknown backend: {kind!r}；合法取值 {VALID_BACKENDS}。"
        f"（不静默退回——配错 backend 会让所有请求落回进程内、毫无隔离）"
    )