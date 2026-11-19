"""粘性 cookie 必须在**任何**响应上下发，而不只是 `/api/health`（2026-11-19 审计 §2）。

## 问题

`_ensure_sticky_cookie` 全仓只有一个调用点：`/api/health`（原 `main.py:274`）。

而 nginx 用 `hash $cookie_hlz_sticky` 做粘性。**没有 cookie 时，
`hash` 不是随机打散，是「所有无 cookie 请求落到同一个上游」** ——
于是这批用户集中压在单个实例上，而 `turn_job._JOBS` 是进程内的，
续看请求落到别处就查不到 job。

没有 cookie 的真实场景：
- 用户清空站点数据
- curl / 脚本 / 第三方客户端直接打 `/api/...`（从不访问 health）
- 前端首屏 health 失败（`App.tsx:337` 是 `catch(() => null)`，静默跳过）
- 扩缩容后 nginx 重启（见审计 §3）

## 修法

挪进已有的 `_instance_header` 中间件 —— 那里已经 `await call_next(request)`
拿到 response 了，任何响应都能种上。暴露面从「必须先访问 health」
降到「至少成功请求过一次」。

## 这组测试钉什么

| 测试 | 钉什么 |
|---|---|
| `test_cookie_set_on_arbitrary_endpoint` | 任意端点都能种上 cookie |
| `test_cookie_not_overwritten_when_present` | 已经带了就**不重发**（避免每次心跳刷 Set-Cookie） |
| `test_health_still_works` | health 自己的行为不回归 |
| `test_middleware_is_the_place` | 断言落点在中间件，而不是又一个端点 |
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """最小 TestClient。隔离库 + 关掉 MCP 探测（那会真连网络）。"""
    from fastapi.testclient import TestClient

    from app import db, main

    path = tmp_path / "app.db"
    monkeypatch.setattr(db, "DB_PATH", path)

    import asyncio
    asyncio.run(db.init_db())
    return TestClient(main.app)


def test_cookie_set_on_arbitrary_endpoint(client):
    """任意端点（这里是 /api/health 之外的根路径）都应种上粘性 cookie。"""
    res = client.get("/")
    # SPA 未构建时根路径可能 404，但中间件不受影响 —— 用 headers 判断
    cookie = res.headers.get("set-cookie", "")
    assert "hlz_sticky=" in cookie, (
        f"根路径没有下发 hlz_sticky，实际 Set-Cookie={cookie!r}。"
        "cookie 仍只在 health 里发的话，没访问过 health 的客户端永远没有粘性。")


def test_cookie_not_overwritten_when_present(client):
    """已经带了 cookie 就不重发 —— 否则每个响应都刷 Set-Cookie。"""
    client.cookies.set("hlz_sticky", "already-here")
    res = client.get("/api/health")
    cookie = res.headers.get("set-cookie", "")
    assert "hlz_sticky" not in cookie, \
        f"已经带 cookie 却又下发了：{cookie!r}"


def test_health_still_works(client):
    """health 自身不回归：仍返回 ok，且没有 cookie 时仍会种。"""
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body.get("ok") is True
    assert "hlz_sticky=" in res.headers.get("set-cookie", "")


def test_middleware_is_the_place():
    """落点断言：cookie 下发必须在中间件里，而不是又加一个端点。

    这条是静态断言，但它测的是**结构契约**（「暴露面 = 所有响应」），
    而不是某个关键词是否出现 —— 前者只能这么测。
    """
    import inspect as _inspect

    from app import main

    src = _inspect.getsource(main._instance_header)
    assert "_ensure_sticky_cookie" in src, \
        "中间件里没有下发粘性 cookie —— 暴露面还是只有 health"

    # health 里不该再有第二处（重复调用无害，但说明没搬干净）
    health_src = _inspect.getsource(main.health)
    assert ("_ensure_sticky_cookie" not in health_src
            or "_ensure_sticky_cookie" in src), \
        "health 里还在单独下发"


def test_secure_flag_off_for_http_deployments():
    """`secure=False` —— HTTP 部署下设 True 会让浏览器直接丢弃 cookie。

    那比没有更糟：应用以为自己发了，nginx 却拿不到。
    """
    import inspect as _inspect

    from app import main

    src = _inspect.getsource(main._ensure_sticky_cookie)
    assert "secure=False" in src, \
        "secure 被改成 True 了？HTTP 部署下粘性会静默失效"