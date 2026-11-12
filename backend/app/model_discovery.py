"""向用户配置的模型端点查询真实可用模型列表。

为什么需要它：前端 SettingsModal 里曾硬编码 10 个「常见模型」短名
（qwen3-max / deepseek-flash …），但那些名字在任何具体网关上都未必存在。
湛卢的真实 ID 是 moma/qwen3.7-max、zhanlu/glm-5.3 这种带前缀的形式，
选了硬编码短名会在实际调用时被网关以 403「权限不足」拒绝——
措辞极具误导性，用户会以为 key 没权限。

所以模型名必须来自端点自己的 /models，而不是我们猜。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 有些网关把 /models 挂在不同的前缀下，按顺序试。
_MODELS_SUFFIXES = ("/models",)
_PROBE_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def _candidate_urls(base_url: str) -> list[str]:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return []
    urls = []
    for suffix in _MODELS_SUFFIXES:
        # base 已含 /v1 之类时直接拼；否则也试一下补 /v1
        urls.append(base + suffix)
    if not base.endswith("/v1"):
        for suffix in _MODELS_SUFFIXES:
            urls.append(base + "/v1" + suffix)
    # 去重保序
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _extract_ids(payload: Any) -> list[dict[str, str]]:
    """兼容三种常见形状：
       {"data": [{"id": "...", "owned_by": "..."}]}   OpenAI 标准
       {"models": [...]}                              部分网关
       ["id1", "id2"]                                 少数网关
    """
    items: list[Any] = []
    if isinstance(payload, dict):
        for key in ("data", "models", "result"):
            v = payload.get(key)
            if isinstance(v, list):
                items = v
                break
    elif isinstance(payload, list):
        items = payload

    out: list[dict[str, str]] = []
    for it in items:
        if isinstance(it, str):
            out.append({"id": it, "owned_by": ""})
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("model") or it.get("name")
            if isinstance(mid, str) and mid.strip():
                out.append({
                    "id": mid.strip(),
                    "owned_by": str(it.get("owned_by") or it.get("ownedBy") or ""),
                })
    # 去重 + 排序，便于前端展示
    seen, dedup = set(), []
    for m in out:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        dedup.append(m)
    dedup.sort(key=lambda x: x["id"].lower())
    return dedup


async def list_remote_models(base_url: str, api_key: str) -> dict[str, Any]:
    """查询端点的真实模型列表。

    返回 {"ok": bool, "models": [...], "error": str, "endpoint": str, "hint": str}
    —— 永远不抛异常，调用方（路由）可以直接把它转成响应。
    """
    if not (base_url or "").strip():
        return {"ok": False, "models": [], "error": "缺少 base_url",
                "endpoint": "", "hint": "先填写 Base URL"}
    if not (api_key or "").strip():
        return {"ok": False, "models": [], "error": "缺少 api_key",
                "endpoint": "", "hint": "必须先填 API Key 才能查询模型列表"}

    headers = {"Authorization": f"Bearer {api_key.strip()}"}
    last_err = ""
    last_status = 0
    tried: list[str] = []

    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
        for url in _candidate_urls(base_url):
            tried.append(url)
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                continue

            last_status = resp.status_code
            if resp.status_code == 200:
                try:
                    payload = resp.json()
                except ValueError:
                    last_err = "响应不是 JSON"
                    continue
                models = _extract_ids(payload)
                if models:
                    return {"ok": True, "models": models, "error": "",
                            "endpoint": url, "hint": ""}
                last_err = "响应里没有解析到模型 ID"
                continue

            # 401/403 换个前缀也没用，直接给出可读提示
            if resp.status_code in (401, 403):
                return {
                    "ok": False, "models": [], "endpoint": url,
                    "error": f"HTTP {resp.status_code}",
                    "hint": ("该端点拒绝了 /models 查询（权限或路径不符）。"
                             "可以直接手填模型 ID；或者确认 Base URL 是否需要带 /v1。"),
                }
            last_err = f"HTTP {resp.status_code}: {resp.text[:160]}"

    return {
        "ok": False,
        "models": [],
        "endpoint": tried[-1] if tried else "",
        "error": last_err or f"HTTP {last_status}" or "未知错误",
        "hint": ("查询失败。很多网关不实现 /models，此时请直接手填模型 ID；"
                 "Base URL 常见形式：https://host/v1"),
    }