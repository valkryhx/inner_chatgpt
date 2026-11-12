"""向用户配置的模型端点查询真实可用模型列表，以及它接受哪些思考等级。

为什么需要它：前端 SettingsModal 里曾硬编码 10 个「常见模型」短名
（qwen3-max / deepseek-flash …），但那些名字在任何具体网关上都未必存在。
湛卢的真实 ID 是 moma/qwen3.7-max、zhanlu/glm-5.3 这种带前缀的形式，
选了硬编码短名会在实际调用时被网关以 403「权限不足」拒绝——
措辞极具误导性，用户会以为 key 没权限。

所以模型名必须来自端点自己的 /models，而不是我们猜。

同理，思考等级（reasoning_effort）也不能猜：不同网关对同一个档位的态度
分成两类，且从成功响应里**看不出来**——
  - 校验型：不认识的档位直接 400/422（官方 DeepSeek 就是这样，并且会在
    错误正文里列出它接受的全部枚举）；
  - 静默丢弃型：整段字段被忽略，任何值都返回 200（湛卢、xem 中转都是）。
判定方法因此只能是「传一个绝对非法的值，看它报不报错」，
见 :func:`probe_reasoning_support`。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 有些网关把 /models 挂在不同的前缀下，按顺序试。
_MODELS_SUFFIXES = ("/models",)
_PROBE_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# 思考等级探测要发一次真实推理请求，给足时间但仍要有上限。
_EFFORT_PROBE_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# 探测用的问题：足够短，尽量少烧 token；只关心 HTTP 状态码。
_EFFORT_PROBE_PROMPT = "hi"
# 绝对非法的档位。任何自称校验该字段的端点都必须拒绝它。
_ILLEGAL_EFFORT = "hlaudz_invalid_probe"
# 我们自己认得的全部档位（与 reasoning.REASONING_LEVELS 保持一致）。
_KNOWN_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


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


# ---------------------------------------------------------------- 思考等级探测

def _wire_path(wire_api: str) -> tuple[str, str]:
    """返回 (路径, 线名)。线名用于决定把 thinking 字段放在哪。"""
    if (wire_api or "").strip().lower() == "openai_chat":
        return "/chat/completions", "chat"
    return "/responses", "responses"


def _effort_payload(line: str, model: str, effort: str) -> dict[str, Any]:
    """按线组装最小请求。两条线的字段位置不同：
    chat 是顶层 reasoning_effort；responses 是 reasoning.effort。
    这里和 llm_client._build_request 必须一致，否则探测结论对线上无效。
    """
    if line == "responses":
        payload: dict[str, Any] = {
            "model": model,
            "input": _EFFORT_PROBE_PROMPT,
            "stream": False,
            "max_output_tokens": 16,
        }
        if effort:
            payload["reasoning"] = {"effort": effort}
        return payload
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _EFFORT_PROBE_PROMPT}],
        "stream": False,
        "max_tokens": 16,
    }
    if effort:
        payload["reasoning_effort"] = effort
    return payload


def _accepted_levels_from_message(text: str) -> list[str]:
    """从「unknown variant `x`, expected one of `a`, `b`」里抠出端点自报的枚举。

    这是最权威的答案：端点自己说它认哪些，比任何文档都准。
    """
    import re

    if not text:
        return []
    m = re.search(r"expected one of\s+(.+)", text, re.IGNORECASE)
    if not m:
        return []
    found = re.findall(r"`([^`]+)`", m.group(1))
    if not found:
        return []
    # 只保留我们认识的档位，保持端点给出的顺序。
    seen: set[str] = set()
    out: list[str] = []
    for raw in found:
        lv = raw.strip().lower()
        if lv in _KNOWN_LEVELS and lv not in seen:
            seen.add(lv)
            out.append(lv)
    return out


async def probe_reasoning_support(base_url: str, api_key: str, model: str, *,
                                  wire_api: str = "openai_responses") -> dict[str, Any]:
    """探测某个模型是否真的接受 reasoning_effort，以及它报告自己认哪些档位。

    判据只有一条：**发一个绝对非法的档位，看端点报不报错**。

    - 报 400/422 → 端点会校验这个字段（``supported=True``）。很多实现会在错误
      正文里列出它接受的枚举，抠出来放进 ``levels``（权威清单）。
    - 返回 200 → 整段字段被忽略（``supported=False``）。这种情况下「传了有效果」
      和「传了没效果」在协议层无法区分，如实告诉用户即可，绝不假装支持。
    - 其他状态（401/403/429/5xx/网络错误）→ 无法判定，``supported=None``。

    永远不抛异常：调用方（路由）可以把返回值直接转成响应。
    """
    base_url = (base_url or "").strip()
    api_key = (api_key or "").strip()
    model = (model or "").strip()
    empty = {"supported": None, "levels": [], "checked_level": _ILLEGAL_EFFORT,
             "error": "", "hint": ""}
    if not base_url:
        return {**empty, "error": "缺少 base_url"}
    if not api_key:
        return {**empty, "error": "缺少 api_key", "hint": "必须先填 API Key 才能探测"}
    if not model:
        return {**empty, "error": "缺少 model",
                "hint": "先选定模型名（来自端点 /models）再探测思考等级"}

    path, line = _wire_path(wire_api)
    url = base_url.rstrip("/") + path
    payload = _effort_payload(line, model, _ILLEGAL_EFFORT)

    async with httpx.AsyncClient(timeout=_EFFORT_PROBE_TIMEOUT) as client:
        try:
            resp = await client.post(
                url, json=payload,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            return {**empty, "error": f"{type(exc).__name__}: {exc}",
                    "hint": "网络不通或端点超时；这不代表模型不支持思考等级"}

    body = resp.text[:2000]
    if resp.status_code in (400, 422):
        levels = _accepted_levels_from_message(body)
        hint = ""
        if not levels:
            hint = ("端点拒绝了这个档位，但没在报错里列出合法枚举；"
                    "请在设置里手动试几档，或设为「不指定」用上游默认。")
        return {
            "supported": True,
            "levels": levels,
            "checked_level": _ILLEGAL_EFFORT,
            "error": "",
            "hint": hint or ("端点会校验 reasoning_effort；上面列出的是它自报的可选档位。"
                             if levels else ""),
        }
    if resp.status_code == 200:
        return {
            "supported": False,
            "levels": [],
            "checked_level": _ILLEGAL_EFFORT,
            "error": "",
            "hint": ("端点接受了非法档位，说明它**整段忽略** reasoning_effort："
                     "在这个端点上，思考等级选哪一档都不会改变上游行为。"
                     "这是网关侧的限制，不是 Hlaudz 的配置问题。"),
        }
    return {**empty, "error": f"HTTP {resp.status_code}: {body[:200]}",
            "hint": ("探测请求被拒绝，无法判断是否支持思考等级。常见原因："
                     "模型名不是该端点的真实 ID、key 无权访问该模型、或额度用尽。")}