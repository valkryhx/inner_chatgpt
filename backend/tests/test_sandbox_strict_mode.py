"""strict 模式的安全语义（TDD RED）。

## 这一组修的是「strict 实际上没有拦截力」

实测（2026-11-19，app 容器内，`HLZ_SANDBOX_MODE=strict` + service 后端）：

    OUT: [code_run python] exit=0 cwd=/app/backend/data/workspaces/u_strict_leak
    file exists? True

沙箱明显不可用，代码却在 app 进程里跑成功了。两个独立缺陷叠加：

### 缺陷 1：可用性探针探错了对象

`manager._degrade_reason()` 调 `probe.check(cfg)`，而 `probe.py` 里
全是 `docker.from_env()`。当前部署走的是 `HLZ_SANDBOX_BACKEND=service`
（HTTP），根本不会用 Docker —— 探针检查的是一个**不会被使用的东西**，
于是永远返回「不可用」。

修法：问**当前选中的后端**能不能干活（`Backend.health()`），
Docker 探针只留给 `inproc` 后端。

### 缺陷 2：裸 except 把 strict 的异常吞了

`builtin_tools.code_run` 里：

```python
except Exception:  # noqa: BLE001 - 沙箱故障不能打断工具本身
    logger.warning("code_run 沙箱分流失败，回落进程内", exc_info=True)
raw = await _run_code_impl(...)   # ← strict 抛的 SandboxUnavailable 被吞后继续执行
```

初衷是「别让沙箱的 bug 弄挂工具」，但它同时把 strict 的**安全承诺**吞了。

### 组合效果（这才是最坏的）

| 模式 | 沙箱挂了时 |
| --- | --- |
| `auto` | 无隔离执行 + **结果首行有「本次未隔离」标记** |
| `strict` | 无隔离执行 + **没有任何提示**（只进日志） |

strict 比 auto 更危险 —— 用户以为选了最严格的档，实际比 auto 少一层可见性。

## 断言口径：看副作用，不看文案

「有没有真的执行」不能看报错文本（`auto` 降级时首行也有「未隔离」字样）。
唯一可靠的判据是**副作用**：让代码写一个带随机 token 的文件，
然后检查它在不在。这也是本仓库「判定送达要用一次性 token」的同一思路。
"""
from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def sandbox_user(monkeypatch):
    """把白名单设空 = 全部用户可进沙箱（`user_in_scope` 空 = 不限制）。

    不设的话默认是 2 人白名单，测试里随机生成的 uid 不在其中，
    `policy.should_use_sandbox()` 直接返回 False，分流分支压根不执行 ——
    测试就变成「验证进程内执行」了，与要测的东西无关（第一版就栽在这）。

    ⚠️ **不在这里 reload**：`get_config` 是进程内单例，fixture 里 reload
    会把「还没设 mode」的状态缓存下来。reload 必须发生在设置完所有 env
    之后 —— 交给 `refresh_config` 在测试体内调用。
    """
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "")
    return None


def refresh_config(mode: str, monkeypatch):
    """设 mode 并让单例重新求值。**顺序很重要**：先 setenv 再 reload。

    ⚠️ 必须同时重建 manager：`get_manager()` 也是进程内单例，它的 `cfg`
    在**构造时**就固定了（`self.cfg = cfg or get_config()`）。只 reload
    config 而不重建 manager，会拿到一个还按旧 mode 判断的 manager ——
    本文件第一版就因此出现了「单跑绿、混跑红」的顺序依赖。
    """
    monkeypatch.setenv("HLZ_SANDBOX_MODE", mode)
    from app.sandbox import config as sbx_config
    from app.sandbox import manager as mgr_mod

    cfg = sbx_config.get_config(reload=True)
    mgr_mod.get_manager(reload=True)
    return cfg


@pytest.fixture()
def ws_root(tmp_path, monkeypatch):
    """把 workspace 根关进 tmp，避免碰真实数据。"""
    from app import config as app_config
    from app import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(app_config, "WORKSPACES_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def tools(ws_root):
    from app import builtin_tools

    def make(uid: str) -> dict:
        items = builtin_tools.build_builtin_tools(uid, include_memory=False)
        return {t.name: t for t in items}

    return make


def _run(tool, **args):
    return asyncio.run(tool.call(args))


# ================================================================ 缺陷 1


def test_degrade_reason_asks_the_selected_backend(monkeypatch):
    """★ 可用性判断必须问**当前后端**，而不是 Docker。

    这条钉的是缺陷 1：service 后端部署下，`probe.check` 探 Docker
    得到的是「另一件东西」的状态。

    ## 为什么要伪造「两边结论相反」

    第一版只写了「后端健康 → reason 应为 None」。那条在**破坏后也绿**：
    本机 Docker 恰好可用时，`probe.check` 同样返回 ok，两种实现
    结论一致 —— 测试分不出它问的是谁（假绿，反向确认抓出来的）。

    所以这里显式让 **Docker 不可用、后端可用**。只有真的去问后端
    才会得到 None；只要还偷偷问 Docker，就会拿到「docker down」。
    """
    from app.sandbox import manager as mgr_mod
    from app.sandbox import probe as probe_mod

    class HealthyService:
        name = "service"

        async def health(self):
            return True, "ok"

        async def exec_code(self, *a, **kw):
            return True, "from-service", ""

        async def write_file(self, *a, **kw):
            return False, ""

        async def patch_file(self, *a, **kw):
            return False, ""

    monkeypatch.setattr(mgr_mod, "_selected_backend", lambda: HealthyService())
    # Docker 侧不可用（与后端结论相反）
    monkeypatch.setattr(probe_mod, "check", lambda cfg: (False, "docker down"))
    monkeypatch.setattr(mgr_mod.probe, "check", lambda cfg: (False, "docker down"))
    refresh_config("strict", monkeypatch)

    m = mgr_mod.get_manager()
    reason = asyncio.run(m._degrade_reason())
    assert reason is None, (
        f"后端健康却判为不可用（reason={reason!r}）——"
        f"探针还在问 Docker，而不是当前后端"
    )


def test_degrade_reason_reports_backend_failure(monkeypatch):
    """后端自检失败时必须返回原因（不能假装健康）。"""
    from app.sandbox import manager as mgr_mod

    class BrokenService:
        name = "service"

        async def health(self):
            return False, "服务返回 500"

        async def exec_code(self, *a, **kw):
            return True, "", ""

        async def write_file(self, *a, **kw):
            return False, ""

        async def patch_file(self, *a, **kw):
            return False, ""

    monkeypatch.setattr(mgr_mod, "_selected_backend", lambda: BrokenService())
    refresh_config("strict", monkeypatch)

    m = mgr_mod.get_manager()
    assert asyncio.run(m._degrade_reason()) == "服务返回 500"


def test_backends_expose_health():
    """三个后端都要有 health()（协议一致性）。

    `off` 永远健康（它什么都不做）；`inproc` 问 Docker；
    `service` 打 HTTP /v1/health。
    """
    from app.sandbox.backends.off import OffBackend

    assert hasattr(OffBackend(), "health"), "OffBackend 缺 health()"


# ================================================================ 缺陷 2


def test_strict_does_not_execute_when_backend_unavailable(ws_root, tools, sandbox_user, monkeypatch):
    """★ 核心：strict + 沙箱不可用 → 用户代码**绝不能**在进程内执行。

    看副作用，不看文案：让代码写一个带随机 token 的文件，
    文件存在 = 降级执行了 = 不安全。
    """
    from app.sandbox.errors import SandboxUnavailable

    refresh_config("strict", monkeypatch)

    from app.sandbox import manager as mgr_mod

    class DownService:
        name = "service"

        async def health(self):
            return False, "连不上"

        async def exec_code(self, *a, **kw):
            raise SandboxUnavailable("连不上")

        async def write_file(self, *a, **kw):
            return False, ""

        async def patch_file(self, *a, **kw):
            return False, ""

    monkeypatch.setattr(mgr_mod, "_selected_backend", lambda: DownService())

    uid = f"u_strict_{secrets.token_hex(3)}"
    token = f"LEAK_{secrets.token_hex(5)}"
    t = tools(uid)[ "code_run"]
    out = _run(t, code=f'open("{token}.txt", "w").write("{token}")')

    leaked = (ws_root / uid / f"{token}.txt").exists()
    assert not leaked, (
        f"strict 下代码仍在 app 进程内执行了（文件 {token}.txt 落盘）。\n"
        f"工具输出：{out[:300]!r}"
    )
    # 而且必须告诉用户失败了，不能静默
    assert "沙箱" in str(out) or "隔离" in str(out), (
        f"strict 失败时必须明确告知，实际输出：{out[:300]!r}"
    )


def test_auto_still_degrades_and_executes(ws_root, tools, sandbox_user, monkeypatch):
    """对照：auto 下同一场景**应该**降级执行（这是它的设计）。

    没有这条对照，上面那条可能因为「代码本来就跑不起来」而假绿。
    """
    from app.sandbox.errors import SandboxUnavailable

    refresh_config("auto", monkeypatch)

    from app.sandbox import manager as mgr_mod

    class DownService:
        name = "service"

        async def health(self):
            return False, "连不上"

        async def exec_code(self, *a, **kw):
            raise SandboxUnavailable("连不上")

        async def write_file(self, *a, **kw):
            return False, ""

        async def patch_file(self, *a, **kw):
            return False, ""

    monkeypatch.setattr(mgr_mod, "_selected_backend", lambda: DownService())

    uid = f"u_auto_{secrets.token_hex(3)}"
    token = f"DEGRADED_{secrets.token_hex(5)}"
    t = tools(uid)["code_run"]
    out = _run(t, code=f'open("{token}.txt", "w").write("{token}")')

    assert (ws_root / uid / f"{token}.txt").exists(), (
        f"auto 应该降级执行，但文件没有落盘。输出：{out[:300]!r}"
    )
    assert "未隔离" in str(out), f"auto 降级必须留痕，实际：{out[:300]!r}"


def test_strict_passes_through_when_backend_healthy(ws_root, tools, sandbox_user, monkeypatch):
    """strict + 后端健康 → 正常走沙箱，不能因为改守卫而误伤正常路径。"""
    refresh_config("strict", monkeypatch)

    from app.sandbox import manager as mgr_mod

    class GoodService:
        name = "service"

        async def health(self):
            return True, "ok"

        async def exec_code(self, *a, **kw):
            return True, "SANDBOX_RAN", ""

        async def write_file(self, *a, **kw):
            return False, ""

        async def patch_file(self, *a, **kw):
            return False, ""

    monkeypatch.setattr(mgr_mod, "_selected_backend", lambda: GoodService())

    uid = f"u_ok_{secrets.token_hex(3)}"
    out = _run(tools(uid)["code_run"], code="print(1)")
    assert "SANDBOX_RAN" in str(out), f"严格模式下健康后端应正常执行，实际：{out[:300]!r}"