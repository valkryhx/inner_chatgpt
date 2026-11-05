"""标准 mcp.json（mcpServers）解析与合并。

支持业界通用格式（与 GenericAgent / Claude Desktop 一致）：
{
  "mcpServers": {
    "name": {
      "type": "stdio" | "http" | "sse",   # 省略时：有 command 视为 stdio，否则 http
      "command": "npx", "args": ["-y", "pkg"], "env": {"K": "V"},
      "url": "https://...", "headers": {"Authorization": "Bearer ..."}
    }
  }
}

合并策略（重要）：按 name upsert —— 已存在的同名覆盖更新，不存在的新增；
**不会删除用户已有的其它 MCP 配置**（增量合并，不清空）。
"""
from __future__ import annotations

import json
from typing import Any

from . import db

VALID_TYPES = {"stdio", "http", "sse"}

def _norm_str_map(v):
    if not isinstance(v, dict):
        return {}
    out = {}
    for k, val in v.items():
        if val is None:
            continue
        out[str(k)] = str(val)
    return out


def _norm_args(v):
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        return v.split() if v.strip() else []
    return []


def normalize_server(name, spec):
    """把一个 mcpServers 条目规范化为内部结构。返回 (dict, error)。"""
    if not isinstance(spec, dict):
        return None, "server '%s' 的值必须是对象" % name
    command = str(spec.get("command") or "").strip()
    url = str(spec.get("url") or "").strip()
    typ = str(spec.get("type") or spec.get("transport") or "").strip().lower()
    if not typ:
        typ = "stdio" if command else "http"
    if typ not in VALID_TYPES:
        return None, "server '%s' 的 type='%s' 不支持（仅 stdio/http/sse）" % (name, typ)
    if typ == "stdio" and not command:
        return None, "stdio server '%s' 缺少 command" % name
    if typ in ("http", "sse") and not url:
        return None, "%s server '%s' 缺少 url" % (typ, name)
    return {
        "name": name,
        "transport": typ,
        "command": command,
        "args": _norm_args(spec.get("args")),
        "url": url,
        "headers": _norm_str_map(spec.get("headers")),
        "env": _norm_str_map(spec.get("env")),
    }, None


def parse_mcp_json(text):
    """解析标准 mcp.json 文本，返回 (servers, errors)。servers 为规范化 dict 列表。"""
    raw = (text or "").strip()
    if not raw:
        return [], ["配置为空"]
    try:
        data = json.loads(raw)
    except Exception as e:
        return [], ["JSON 解析失败: %s" % e]
    if isinstance(data, dict) and "mcpServers" in data:
        servers_obj = data.get("mcpServers")
    elif isinstance(data, dict) and all(isinstance(v, dict) for v in data.values()) and data:
        # 容错：用户直接粘贴了 mcpServers 的内容（省略外层）
        servers_obj = data
    else:
        return [], ["缺少 mcpServers 对象"]
    if not isinstance(servers_obj, dict) or not servers_obj:
        return [], ["mcpServers 必须是非空对象"]
    servers, errors = [], []
    for name, spec in servers_obj.items():
        s, err = normalize_server(str(name), spec)
        if err:
            errors.append(err)
        else:
            servers.append(s)
    return servers, errors

async def merge_mcp_json(user_id, text, enabled=True):
    """解析并增量合并到用户 MCP 配置（按 name upsert，不删除既有其它配置）。"""
    servers, errors = parse_mcp_json(text)
    existing = await db.list_mcp_servers(user_id)
    by_name = {s.get("name"): s for s in existing}
    added, updated = [], []
    for s in servers:
        name = s["name"]
        payload = {
            "name": name,
            "transport": s["transport"],
            "command": s["command"],
            "args": s["args"],
            "url": s["url"],
            "headers": s["headers"],
            "env": s["env"],
            "enabled": bool(enabled),
        }
        cur = by_name.get(name)
        if cur:
            await db.update_mcp_server(user_id, cur["id"], **payload)
            updated.append(name)
        else:
            await db.create_mcp_server(user_id, **payload)
            added.append(name)
    total = len(await db.list_mcp_servers(user_id))
    msg = "解析到 %d 个 server：新增 %d，更新 %d；其它既有配置未改动" % (len(servers), len(added), len(updated))
    if errors:
        msg += "；忽略 %d 个无效项" % len(errors)
    return {
        "ok": len(servers) > 0,
        "added": added, "updated": updated, "skipped": [], "errors": errors,
        "total_servers": total, "message": msg,
    }


def server_to_spec(s):
    """把一条内部记录还原成 mcpServers 里的 spec（用于前端回显）。"""
    typ = s.get("transport") or "http"
    spec = {"type": typ}
    if typ == "stdio":
        spec["command"] = s.get("command") or ""
        if s.get("args"):
            spec["args"] = s["args"]
        if s.get("env"):
            spec["env"] = s["env"]
    else:
        spec["url"] = s.get("url") or ""
        if s.get("headers"):
            spec["headers"] = s["headers"]
    return spec


def servers_to_mcp_json(rows):
    """把用户全部 MCP 记录还原为标准 mcp.json 文本（给前端编辑框回显）。"""
    obj = {"mcpServers": {}}
    for s in rows:
        obj["mcpServers"][s.get("name") or s.get("id")] = server_to_spec(s)
    return json.dumps(obj, ensure_ascii=False, indent=2)

