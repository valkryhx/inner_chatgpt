"""执行器接口：所有运行时差异都藏在这一个 Protocol 后面。

## 为什么要有这层

方案选 A（先 bwrap，后补 gVisor）。选 A 的唯一技术前提是
**执行层可替换**——阶段 4 上 gVisor 时应该只新增 `gvisor.py` + 一个环境变量，
不动 HTTP 层 / 池 / 归档 / app 侧客户端（方案 §8.2.3）。

如果将来换实现时要改别的地方，说明这个接口没抽对。

## 三个实现

- `bwrap.py`    —— 现在用的（不需要 docker.sock）
- `gvisor.py`   —— 将来用户变不可信时（需要 runsc + /dev/kvm）
- `inproc.py`   —— `HLZ_SANDBOX_MODE=off` / 回滚路径
"""
from __future__ import annotations

from typing import NamedTuple, Protocol, runtime_checkable


class ExecResult(NamedTuple):
    """一次执行的结果。

    用 NamedTuple 而不是 dataclass：它要跨线程（`asyncio.to_thread`）返回，
    不可变更安全；而且 gVisor 实现复用同一结构时不会漏字段。
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool


@runtime_checkable
class Executor(Protocol):
    """执行用户代码的唯一边界。

    所有运行时差异（bwrap / gVisor / runc）都藏在这后面。
    上层只依赖它，不知道底下是什么。
    """

    async def start(self, user_id: str) -> None:
        """准备该用户的执行环境（预热 / 建沙箱 / 起容器）。

        幂等：重复调用不报错。
        """
        ...

    async def exec(
        self,
        user_id: str,
        code: str,
        *,
        code_type: str = "python",
        timeout_s: float = 120.0,
        cwd: str = "",
    ) -> ExecResult:
        """执行一段代码。`user_id` 决定用哪个 workspace。"""
        ...

    async def destroy(self, user_id: str) -> None:
        """回收该用户的执行环境。

        只清执行环境，**不负责归档**——归档是上层的事（方案 §7.5），
        因为归档后端是可切换的（卷 / S3），不该耦合进执行器。
        """
        ...

    async def health(self) -> tuple[bool, str]:
        """自检。返回 (ok, reason)。

        `reason` 在 ok=False 时要能让人看懂哪里坏了（不是 "error"）。
        """
        ...