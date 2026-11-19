"""本地 MCP 服务不能被系统代理拦（Windows 实测复现）。

## 事故（2026-11-19，hx 用户的三个 MCP）

`sqlbotmcp`（`http://127.0.0.1:8765/mcp`）与 `falcomm`
（`http://127.0.0.1:8889/client/mcp`）都报：

```
HTTPStatusError: Server error '502 Bad Gateway'
```

看起来像「MCP 服务挂了」，实际是本机 Windows 系统代理开着：

```
reg query "HKCU\\...\\Internet Settings"
  ProxyEnable  0x1
  ProxyServer  127.0.0.1:7897      <- Clash / 类似工具
```

httpx 的 `trust_env=True`（默认）会把 **127.0.0.1 的请求也送进代理**。
代理连不上那个上游端口就返回 502 —— 于是「服务没起」被伪装成
「服务返回 502」，误导排查方向（会去查 MCP 服务本身的日志）。

实测对照（对同一个不存在的端口 9999）：

| 配置 | 结果 |
| --- | --- |
| 走代理（默认） | `502 Bad Gateway` ← **假象** |
| 直连 | `ConnectError: [WinError 10061] 目标计算机积极拒绝` ← 真相 |

## 修法

本地地址（`127.0.0.1` / `localhost` / `::1`）的连接**显式绕过代理**。
不全局关 `trust_env` —— 那会让外网 MCP（exa）在需要代理的环境里连不上。

## 反向确认

把 `_is_local_url` 改成恒 `False`，`test_local_url_bypasses_proxy` 必须变红。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


# ---------------------------------------------------------------- 判定函数

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765/mcp",
    "http://127.0.0.1:8889/client/mcp",
    "http://localhost:8000/mcp",
    "http://LOCALHOST:8000/mcp",
    "http://[::1]:8000/mcp",
    "http://127.0.0.1/mcp",
    "https://127.0.0.1:8443/mcp",
])
def test_local_urls_detected(url):
    from app.mcp_client import _is_local_url
    assert _is_local_url(url) is True, f"{url} 应判为本地地址"


@pytest.mark.parametrize("url", [
    "https://mcp.exa.ai/mcp",
    "http://192.168.1.10:8765/mcp",
    "http://10.0.0.5/mcp",
    "http://example.com/mcp",
    "http://127.0.0.1.nip.io/mcp",       # 域名前缀像本地但不是
    "",
])
def test_non_local_urls_not_detected(url):
    from app.mcp_client import _is_local_url
    assert _is_local_url(url) is False, f"{url} 不该判为本地地址"


def test_local_url_bypasses_proxy():
    """**核心**：本地 URL 造出来的 transport 必须挂上「不走代理」的 httpx factory。

    fastmcp 的 transport 只通过 `httpx_client_factory` 定制底层 httpx
    （没有直接暴露 `trust_env`），所以断言点在 factory 是否存在 +
    它真的产出 trust_env=False 的 client。
    """
    from app import mcp_client

    t = mcp_client._build_target(
        {"transport": "http", "url": "http://127.0.0.1:8765/mcp"})
    factory = getattr(t, "httpx_client_factory", None)
    assert factory is not None, "本地 transport 没有挂 httpx_client_factory（会走系统代理）"

    # 真造一个 client，确认 trust_env 被关掉
    import asyncio
    client = factory()
    try:
        assert client._trust_env is False, (
            "本地 client 仍会读系统代理 —— 本地 MCP 会被代理拦成 502")
    finally:
        # AsyncClient 必须用 aclose（没有同步 close）
        asyncio.run(client.aclose())


def test_local_factory_matches_http_client_kwargs():
    """factory 的意图必须与 `_http_client_kwargs` 一致（一处定义，两处使用）。"""
    from app.mcp_client import _http_client_kwargs
    for url in ("http://127.0.0.1:8765/mcp", "http://localhost:8889/client/mcp"):
        assert _http_client_kwargs(url).get("trust_env") is False


def test_remote_url_keeps_env_proxy():
    """外网 MCP（exa）必须保留 fastmcp 默认的 httpx（不强制关代理）。

    否则在需要代理才能访问外网的环境里，exa 会连不上。
    """
    from app.mcp_client import _build_target

    t = _build_target({"transport": "http", "url": "https://mcp.exa.ai/mcp"})
    assert getattr(t, "httpx_client_factory", None) is None, (
        "外网 transport 被强制挂上了自定义 factory（可能破坏了代理）")


# ---------------------------------------------------------------- 真实行为

def test_local_connection_refused_not_masked_as_502():
    """**核心行为**：连不存在的本地端口，错误必须是 ConnectError 而不是 502。

    502 是代理给的假象；ConnectError 才是「服务没起」的真相。
    这条会真的发一次请求（端口 9999 上没有任何服务）。
    """
    import httpx

    from app.mcp_client import _http_client_kwargs

    kwargs = _http_client_kwargs("http://127.0.0.1:9999/mcp")
    assert kwargs.get("trust_env") is False, f"本地地址没关代理：{kwargs}"
    with pytest.raises(httpx.ConnectError):
        with httpx.Client(timeout=4, **kwargs) as c:
            c.post("http://127.0.0.1:9999/mcp", json={})


def test_http_client_kwargs_for_remote_has_no_forced_bypass():
    from app.mcp_client import _http_client_kwargs
    assert "trust_env" not in _http_client_kwargs("https://mcp.exa.ai/mcp")