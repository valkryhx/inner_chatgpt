"""阶段 2：app 侧按 `HLZ_SANDBOX_BACKEND` 选后端的测试（TDD RED）。

## 这一层要守住什么

1. **`mode=off` 时一个 HTTP 请求都不发**（方案 §6.4）——
   判定必须是「网络层 0 次调用」，不是「输出正确」。
   输出对了但偷偷发请求，同样违规（多一次跨服务依赖）。

2. **回滚路径必须真的能回滚**（方案 §6.3）——
   `backend=inproc` 时走现有 Docker 实现，一行不动。

3. **降级语义不变**（方案 §6.1）——
   `auto` + 服务不可达 → 退回进程内 + 首行「本次未隔离」；
   `strict` + 服务不可达 → 抛错。

## 为什么用假 httpx 而不是真起服务

单测不该依赖网络。真服务在 `docs/probes/20261119-sandbox-service-e2e.py` 里验。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture(autouse=True)
def _clear_caches():
    """每个测试前后都清 sandbox 的配置缓存，避免 env 串味。"""
    from app.sandbox import config as sbx_config

    sbx_config.get_config(reload=True)
    yield
    sbx_config.get_config(reload=True)


# ---------------------------------------------------------------- off 模式


def test_off_mode_never_issues_http(monkeypatch):
    """★ 核心兼容断言：mode=off 时，即使服务地址配了也**不许**发请求。

    如果这条红了，说明「关掉沙箱」不再是真的关掉——那会破坏
    方案 §6.1 里「off 分支必须逐字节保持现状」的硬要求。
    """
    calls: list = []

    class BoomClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            calls.append("enter")
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            calls.append(("post", a))
            raise AssertionError("off 模式不许发 HTTP 请求")

    monkeypatch.setenv("HLZ_SANDBOX_MODE", "off")
    monkeypatch.setenv("HLZ_SANDBOX_SERVICE_URL", "http://should-never-be-called:8021")
    monkeypatch.setenv("HLZ_SANDBOX_SERVICE_TOKEN", "t")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", BoomClient)

    from app.sandbox import config as sbx_config

    cfg = sbx_config.get_config(reload=True)
    assert cfg.mode == "off"
    # off 时 enabled 为 False，调用方根本不会进沙箱分支
    assert cfg.enabled is False
    assert calls == [], f"off 模式发起了 HTTP: {calls}"


def test_off_mode_does_not_instantiate_executor(monkeypatch):
    """off 时返回 OffBackend，而不是任何会碰网络的实现。

    判定方式刻意简单：**返回值的类型**就是证据。
    用 spy 去监控「有没有构造客户端」反而绕——因为 OffBackend
    根本没有可以构造客户端的地方，类型本身就说明了。
    """
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "off")
    monkeypatch.setenv("HLZ_SANDBOX_BACKEND", "service")  # 即使配了 service

    from app.sandbox import backends

    b = backends.get_backend(reload=True)
    assert type(b).__name__ == "OffBackend"
    assert b.name == "off"


def test_off_backend_returns_handled_false(monkeypatch):
    """OffBackend.exec_code 必须 handled=False，且**不带**降级提示。

    off 是正常状态，不是「降级」——带提示会让工具结果出现
    「本次未隔离」这种让用户困惑的话（他本来就没开沙箱）。
    """
    from app.sandbox.backends.off import OffBackend

    import asyncio

    handled, out, marker = asyncio.run(OffBackend().exec_code("u1", "print(1)"))
    assert handled is False
    assert out == ""
    assert marker == ""


# ---------------------------------------------------------------- 后端选择


def test_backend_service_selected(monkeypatch):
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HLZ_SANDBOX_BACKEND", "service")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "")

    from app.sandbox import backends

    b = backends.get_backend(reload=True)
    assert b.name == "service"


def test_backend_inproc_selected_for_rollback(monkeypatch):
    """回滚路径：一个环境变量切回现有 Docker 实现（方案 §6.3 硬要求）。"""
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HLZ_SANDBOX_BACKEND", "inproc")

    from app.sandbox import backends

    b = backends.get_backend(reload=True)
    assert b.name == "inproc"


def test_unknown_backend_is_rejected(monkeypatch):
    """未知 backend 必须报错，不静默退回 inproc。

    静默退回会让「配错了但看起来在工作」，而 `service` 配错的后果是
    **所有请求都落回进程内、毫无隔离**——比直接报错危险得多。
    """
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HLZ_SANDBOX_BACKEND", "nonsense")

    from app.sandbox import backends

    with pytest.raises(ValueError, match="unknown"):
        backends.get_backend(reload=True)


# ---------------------------------------------------------------- 降级语义


def test_auto_mode_degrades_when_service_unreachable(monkeypatch):
    """auto + 服务不可达 → 返回 handled=False + 降级说明（不抛错）。"""
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HLZ_SANDBOX_BACKEND", "service")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "")

    from app.sandbox import backends

    b = backends.get_backend(reload=True)

    import asyncio

    async def boom(*a, **kw):
        raise ConnectionError("service down")

    b.exec = boom  # type: ignore[method-assign]

    handled, out, marker = asyncio.run(b.exec_code("u1", "print(1)"))
    assert handled is False
    assert "未隔离" in out, out


def test_strict_mode_fails_when_service_unreachable(monkeypatch):
    """strict + 服务不可达 → 抛错，不降级。"""
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "strict")
    monkeypatch.setenv("HLZ_SANDBOX_BACKEND", "service")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "")

    from app.sandbox import backends

    b = backends.get_backend(reload=True)

    import asyncio

    async def boom(*a, **kw):
        raise ConnectionError("service down")

    b.exec = boom  # type: ignore[method-assign]

    from app.sandbox.errors import SandboxUnavailable

    with pytest.raises(SandboxUnavailable):
        asyncio.run(b.exec_code("u1", "print(1)"))


# ---------------------------------------------------------------- service 客户端


def test_service_client_sends_required_headers(monkeypatch):
    """调服务时必须带 Authorization + X-HLZ-User。

    X-HLZ-User 是 app **服务端**填的（app 已经认证过用户），
    不是模型的工具参数（方案 §4.4 约束 1）。
    """
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HLZ_SANDBOX_BACKEND", "service")
    monkeypatch.setenv("HLZ_SANDBOX_SERVICE_URL", "http://svc:8021")
    monkeypatch.setenv("HLZ_SANDBOX_SERVICE_TOKEN", "secret-token")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "")

    from app.sandbox.backends import service as svc_mod

    captured: dict = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "exit_code": 0, "stdout": "ok", "stderr": "",
                "duration_ms": 5, "truncated": False,
            }

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers or {}
            return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    b = svc_mod.ServiceBackend()
    import asyncio

    handled, out, marker = asyncio.run(b.exec_code("u_alice", "print(1)"))

    assert handled is True
    assert captured["url"].endswith("/v1/exec")
    hdrs = {k.lower(): v for k, v in captured["headers"].items()}
    assert hdrs.get("authorization") == "Bearer secret-token"
    assert hdrs.get("x-hlz-user") == "u_alice"
    # 请求体里不该有 user_id（身份只走 header）
    assert "user_id" not in (captured["json"] or {})


def test_service_client_maps_504_to_timeout_message(monkeypatch):
    """504 要变成可读的超时说明，不能变成「未知错误」。"""
    monkeypatch.setenv("HLZ_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HLZ_SANDBOX_BACKEND", "service")
    monkeypatch.setenv("HLZ_SANDBOX_SERVICE_URL", "http://svc:8021")
    monkeypatch.setenv("HLZ_SANDBOX_SERVICE_TOKEN", "t")
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "")

    from app.sandbox.backends import service as svc_mod

    class FakeResp:
        status_code = 504

        def json(self):
            return {"detail": {"message": "执行超过 60s 被终止", "stdout": "partial", "stderr": ""}}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    b = svc_mod.ServiceBackend()
    import asyncio

    handled, out, _ = asyncio.run(b.exec_code("u1", "sleep"))
    assert handled is True      # 超时是**沙箱执行过**的结果，不是降级
    assert "超时" in out or "终止" in out, out