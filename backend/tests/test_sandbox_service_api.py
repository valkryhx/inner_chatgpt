"""沙箱服务 HTTP 层的测试（TDD RED）。

## 这一层要守住什么

1. **`user_id` 只能来自服务端认证上下文**，不能只信请求体（方案 §4.4 接口草案的约束 1）
   —— 这是多租户的底线。方案 §2.5.1 里 GPT 讨论文档的 A.7-2 说得对：
   「用户身份与执行边界不能由模型决定」。
2. **错误要可区分**：超时 / 越界 / 执行失败不能都返回 500。
3. **`/v1/health` 要如实报告** executor 状态。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def fake_executor(monkeypatch, tmp_path):
    """替换成假执行器，避免测试依赖 bwrap。"""
    from sandbox_service.executor.base import ExecResult

    class FakeExecutor:
        def __init__(self):
            self.calls: list[tuple[str, str]] = []
            self.started: set[str] = set()

        async def start(self, user_id):
            self.started.add(user_id)

        async def exec(self, user_id, code, *, code_type="python", timeout_s=120.0, cwd=""):
            self.calls.append((user_id, code))
            return ExecResult(
                exit_code=0,
                stdout=f"ran {code}",
                stderr="",
                duration_ms=1,
                truncated=False,
            )

        async def destroy(self, user_id):
            self.started.discard(user_id)

        async def health(self):
            return True, "ok"

    fake = FakeExecutor()

    from sandbox_service import main as svc_main

    monkeypatch.setattr(svc_main, "get_executor", lambda **kw: fake)
    return fake


@pytest.fixture()
def client(fake_executor, monkeypatch, tmp_path):
    from sandbox_service import main as svc_main

    monkeypatch.setenv("HLZ_SANDBOX_WORKSPACES_DIR", str(tmp_path))
    from sandbox_service import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "DEFAULT_WORKSPACES_DIR", tmp_path)

    svc_main.app.dependency_overrides[svc_main.authenticate] = lambda: "u_authed"
    with TestClient(svc_main.app) as c:
        yield c
    svc_main.app.dependency_overrides.clear()


def test_health_reports_ok(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_exec_returns_structured_result(client, fake_executor):
    r = client.post("/v1/exec", json={"code": "print(1)", "code_type": "python"})
    assert r.status_code == 200
    body = r.json()
    assert body["exit_code"] == 0
    assert "ran print(1)" in body["stdout"]
    assert "duration_ms" in body


def test_exec_uses_authenticated_user_not_body(client, fake_executor):
    """★ 核心安全断言：请求体里的 user_id 不能覆盖认证身份。

    如果这个测试红了，说明多租户边界可以被一个 JSON 字段打穿。

    ⚠️ 分两种情形，都要验：
      a) 不带 user_id  → 用认证身份
      b) 带了且不一致  → 403（见下一条测试），**不能**按请求体的走
    """
    r = client.post("/v1/exec", json={"code": "print(1)"})
    assert r.status_code == 200
    assert fake_executor.calls[0][0] == "u_authed"


def test_exec_rejects_when_user_id_in_body_conflicts(client):
    """请求体带 user_id 且与认证身份不符时，直接 403（不静默忽略）。

    静默忽略看着更"宽容"，但会让调用方以为自己的身份被接受了，
    出问题时极难排查。
    """
    r = client.post(
        "/v1/exec",
        json={"code": "print(1)", "user_id": "u_somebody_else"},
    )
    assert r.status_code == 403


def test_exec_rejects_empty_code(client):
    r = client.post("/v1/exec", json={"code": "   "})
    assert r.status_code == 422


def test_exec_rejects_unknown_code_type(client):
    r = client.post("/v1/exec", json={"code": "print(1)", "code_type": "ruby"})
    assert r.status_code == 422


def test_exec_rejects_oversized_code(client):
    """1 MiB 上限（照 Aivory 的 MAX_CODE_BYTES 做法）。"""
    r = client.post("/v1/exec", json={"code": "x = 1\n" * 300_000})
    assert r.status_code == 413


def test_exec_rejects_negative_timeout(client):
    r = client.post("/v1/exec", json={"code": "print(1)", "timeout_s": -5})
    assert r.status_code == 422


def test_exec_clamps_huge_timeout(client, fake_executor):
    """超大 timeout 要被夹到上限，不能任由调用方要 10 小时。"""
    r = client.post("/v1/exec", json={"code": "print(1)", "timeout_s": 999_999})
    assert r.status_code == 200


def test_exec_maps_timeout_to_504(client, monkeypatch):
    """执行超时要映射成 504，不是 500——调用方据此决定要不要重试。"""
    from sandbox_service.executor.base import ExecResult

    class TimeoutExecutor:
        async def start(self, uid): ...
        async def destroy(self, uid): ...
        async def health(self): return True, "ok"
        async def exec(self, uid, code, **kw):
            return ExecResult(124, "", "timeout", 1, False)

    from sandbox_service import main as svc_main

    monkeypatch.setattr(svc_main, "get_executor", lambda **kw: TimeoutExecutor())
    # `_get_executor()` 是单例（daemon 必须挂在同一个实例上）。替换工厂后要清掉
    # 缓存，否则拿到的还是上一个测例留下的实例——本文件这两条曾经因此假绿。
    svc_main._reset_executor()
    r = client.post("/v1/exec", json={"code": "sleep"})
    assert r.status_code == 504


def test_exec_maps_path_escape_to_400(client, monkeypatch):
    """workspace 路径越界要 400，不是 500。"""
    from sandbox_service.executor.base import ExecResult

    class EscapeExecutor:
        async def start(self, uid): ...
        async def destroy(self, uid): ...
        async def health(self): return True, "ok"
        async def exec(self, uid, code, **kw):
            from sandbox_service.workspace import PathSecurityError

            raise PathSecurityError("路径越界")

    from sandbox_service import main as svc_main

    monkeypatch.setattr(svc_main, "get_executor", lambda **kw: EscapeExecutor())
    svc_main._reset_executor()      # 同 test_exec_maps_timeout_to_504：清单例缓存
    r = client.post("/v1/exec", json={"code": "print(1)"})
    assert r.status_code == 400


def test_exec_requires_auth(monkeypatch):
    """没有认证头时必须 401。

    注意顺序：token **未配置**时是 503（拒绝服务），配了但没带 header 才是 401。
    这条测的是后者，所以要先把 token 设上。
    """
    monkeypatch.setenv("HLZ_SANDBOX_SERVICE_TOKEN", "test-token")
    from sandbox_service import main as svc_main

    svc_main.app.dependency_overrides.clear()
    with TestClient(svc_main.app) as c:
        r = c.post("/v1/exec", json={"code": "print(1)"})
    assert r.status_code == 401


def test_exec_returns_503_when_token_unconfigured(monkeypatch):
    """token 没配时拒绝服务，不是放行。

    照 Aivory 的做法：sidecar 没 key 就拒绝启动（它的 compose 里明写
    「the sidecar refuses to start without it」）。这里选择运行期 503，
    因为服务本身还要能响应 /v1/health 让运维看到问题。
    """
    monkeypatch.delenv("HLZ_SANDBOX_SERVICE_TOKEN", raising=False)
    from sandbox_service import main as svc_main

    svc_main.app.dependency_overrides.clear()
    with TestClient(svc_main.app) as c:
        r = c.post(
            "/v1/exec",
            json={"code": "print(1)"},
            headers={"Authorization": "Bearer whatever", "X-HLZ-User": "u1"},
        )
    assert r.status_code == 503
    assert "TOKEN" in r.json()["detail"]


def test_health_does_not_require_auth(client):
    """健康检查不要求认证（供容器 healthcheck 用）。"""
    from sandbox_service import main as svc_main

    svc_main.app.dependency_overrides.clear()
    with TestClient(svc_main.app) as c:
        r = c.get("/v1/health")
    assert r.status_code == 200

# ---------------------------------------------------------------- 池接线


def test_exec_acquires_and_releases_slot(client, fake_executor, monkeypatch):
    """exec 必须经过池（acquire + release）。

    如果这条红了，说明池没接上——所有扩缩容配置都是摆设。
    """
    from sandbox_service import main as svc_main

    calls: list[str] = []

    class SpyPool:
        max_slots = 3

        async def acquire(self, user_id):
            calls.append(f"acquire:{user_id}")

            class S:
                user_id_ = user_id
                workspace = tmp_ws
            s = S()
            s.user_id = user_id
            return s

        async def release(self, user_id):
            calls.append(f"release:{user_id}")

        async def reap_idle(self):
            return 0

    import pathlib
    import tempfile

    tmp_ws = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(svc_main, "_get_pool", lambda: SpyPool())
    monkeypatch.setattr(svc_main, "_maybe_restore", lambda slot: asyncio.sleep(0))

    import asyncio

    r = client.post("/v1/exec", json={"code": "print(1)"})
    assert r.status_code == 200
    assert calls == ["acquire:u_authed", "release:u_authed"], calls


def test_slot_busy_maps_to_503_with_wait_ms(client, monkeypatch):
    """槽满（SlotBusy）要映射成 503，并带上 queue_wait_ms。

    带 queue_wait_ms 是为了让「要不要调大 MAX_SLOTS」有数据支撑（方案 §4.7.4）。
    """
    from sandbox_service import main as svc_main
    from sandbox_service.pool import SlotBusy

    class BusyPool:
        max_slots = 1

        async def acquire(self, user_id):
            raise SlotBusy("沙箱繁忙", queue_wait_ms=1234)

        async def release(self, user_id):
            pass

        async def reap_idle(self):
            return 0

    monkeypatch.setattr(svc_main, "_get_pool", lambda: BusyPool())

    r = client.post("/v1/exec", json={"code": "print(1)"})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["queue_wait_ms"] == 1234
    assert "繁忙" in detail["message"]
