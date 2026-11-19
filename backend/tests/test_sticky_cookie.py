"""粘性 cookie 下发。

## 为什么需要

`deploy/nginx.conf` 用 `hash $cookie_hlz_sticky consistent` 做粘性。
nginx 的 `hash` 指令**只能读 cookie，无法设置** —— 应用必须自己下发一个
稳定的随机值，否则所有请求的 cookie 都是空串，哈希到同一个上游：
负载均衡完全失效（但不会报错，所以是典型的「静默失效」）。

## 三个必须成立的性质

1. 第一次请求下发
2. 已带 cookie 的请求**不重复下发**（否则每次心跳都刷 Set-Cookie）
3. 值是随机的（不是 user_id / IP 之类可预测、可共用标识）

## 反向确认

把 `_ensure_sticky_cookie` 里的 `if request.cookies.get(...): return` 删掉，
`test_second_request_does_not_reset_cookie` 必须变红。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_first_request_sets_sticky_cookie(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    raw = r.headers.get("set-cookie", "")
    assert "hlz_sticky=" in raw, "没有下发粘性 cookie —— 负载均衡会静默失效"


def test_cookie_attributes(client):
    """HttpOnly + Path=/ + 足够长的 Max-Age。

    HttpOnly：前端不需要读它，暴露给 JS 只会增加 XSS 面。
    Path=/：必须覆盖整个站点，否则 /api/* 请求带不上。
    """
    r = client.get("/api/health")
    raw = r.headers.get("set-cookie", "").lower()
    assert "httponly" in raw
    assert "path=/" in raw
    assert "max-age=" in raw


def test_second_request_does_not_reset_cookie(client):
    """已带 cookie 时不能再下发 —— 否则每次心跳都刷 header。"""
    r1 = client.get("/api/health")
    assert "hlz_sticky=" in r1.headers.get("set-cookie", "")
    r2 = client.get("/api/health")
    assert "hlz_sticky=" not in r2.headers.get("set-cookie", ""), \
        "重复下发了粘性 cookie（每个请求都在刷 Set-Cookie）"


def test_cookie_value_is_random_not_identity(client):
    """值必须是随机的，不能是 user_id / IP / 固定串。

    两个独立客户端应该拿到不同的值（同一个值意味着所有人粘到同一实例）。
    """
    from fastapi.testclient import TestClient
    from app.main import app

    a = TestClient(app).get("/api/health").headers.get("set-cookie", "")
    b = TestClient(app).get("/api/health").headers.get("set-cookie", "")

    def value(raw: str) -> str:
        for part in raw.split(";"):
            if part.strip().startswith("hlz_sticky="):
                return part.strip().split("=", 1)[1]
        return ""

    va, vb = value(a), value(b)
    assert va and vb
    assert va != vb, "两个客户端拿到同一个粘性值 —— 值不是随机的？"
    assert len(va) >= 16, f"值太短（{len(va)}），碰撞概率高"


def test_health_still_works_with_cookie_present(client):
    """带了 cookie 的请求，健康检查本体必须照常返回。"""
    client.get("/api/health")
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_sticky_cookie_name_matches_nginx_config():
    """cookie 名必须与 nginx.conf 里的 `hash $cookie_hlz_sticky` 完全一致。

    改了名字而没改 nginx，粘性会静默失效（空 cookie 哈希到固定上游）。
    """
    from app.main import STICKY_COOKIE

    conf = (BACKEND / "deploy" / "nginx.conf").read_text(encoding="utf-8")
    assert f"$cookie_{STICKY_COOKIE}" in conf, (
        f"nginx.conf 没有引用 $cookie_{STICKY_COOKIE} —— "
        f"两边名字对不上，粘性会静默失效")