"""用户设置路由：用户级 LLM 配置 + 用户级 MCP 服务器（均按 user_id 隔离）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import db
from ..auth import get_current_user
from ..schemas import (
    LLMConfigIn,
    LLMConfigOut,
    LLMConfigUpdate,
    MCPImportResult,
    MCPJsonIn,
    MCPServerIn,
    MCPServerOut,
    MCPServerUpdate,
)

router = APIRouter(prefix="/api/me", tags=["me"])


def _mask(api_key: str) -> str:
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "****"
    return api_key[:4] + "****" + api_key[-4:]


def _llm_out(row: dict) -> LLMConfigOut:
    key = row.get("api_key", "") or ""
    return LLMConfigOut(
        id=row["id"],
        label=row.get("label", ""),
        wire_api=row.get("wire_api", ""),
        base_url=row.get("base_url", ""),
        api_key_masked=_mask(key),
        has_api_key=bool(key),
        model=row.get("model", ""),
        context_window=int(row.get("context_window", 128000)),
        max_tokens=int(row.get("max_tokens", 8192)),
        auto_compact_tokens=row.get("auto_compact_tokens"),
        hard_limit_tokens=row.get("hard_limit_tokens"),
        reasoning_effort=row.get("reasoning_effort", "") or "",
        is_default=bool(row.get("is_default")),
    )


def _mcp_out(row: dict) -> MCPServerOut:
    return MCPServerOut(
        id=row["id"],
        name=row.get("name", ""),
        transport=row.get("transport", "http"),
        command=row.get("command", ""),
        args=row.get("args", []) or [],
        url=row.get("url", ""),
        headers=row.get("headers", {}) or {},
        env=row.get("env", {}) or {},
        enabled=bool(row.get("enabled")),
    )


# ---------------- LLM configs ----------------
@router.get("/llm-configs", response_model=list[LLMConfigOut])
async def list_llm(user: dict = Depends(get_current_user)):
    rows = await db.list_llm_configs(user["id"])
    return [_llm_out(r) for r in rows]


@router.post("/llm-configs", response_model=LLMConfigOut)
async def create_llm(body: LLMConfigIn, user: dict = Depends(get_current_user)):
    from ..runtime import get_hub
    row = await db.create_llm_config(user["id"], **body.model_dump())
    hub = get_hub()
    if hub:
        await hub.invalidate(user["id"])
    return _llm_out(row)


@router.patch("/llm-configs/{config_id}", response_model=LLMConfigOut)
async def update_llm(config_id: str, body: LLMConfigUpdate, user: dict = Depends(get_current_user)):
    from ..runtime import get_hub
    # exclude_unset：只更新显式提交的字段；阈值显式传 null 表示清空（按窗口比例派生）。
    fields = body.model_dump(exclude_unset=True)
    for _k in ("auto_compact_tokens", "hard_limit_tokens"):
        if _k in fields:
            fields[_k] = db._opt_int(fields[_k])
    row = await db.update_llm_config(user["id"], config_id, **fields)
    if not row:
        raise HTTPException(status_code=404, detail="config not found")
    hub = get_hub()
    if hub:
        await hub.invalidate(user["id"])
    return _llm_out(row)


@router.delete("/llm-configs/{config_id}")
async def delete_llm(config_id: str, user: dict = Depends(get_current_user)):
    from ..runtime import get_hub
    ok = await db.delete_llm_config(user["id"], config_id)
    if not ok:
        raise HTTPException(status_code=404, detail="config not found")
    hub = get_hub()
    if hub:
        await hub.invalidate(user["id"])
    return {"ok": True}


# ---------------- MCP servers ----------------
@router.get("/mcp-servers", response_model=list[MCPServerOut])
async def list_mcp(user: dict = Depends(get_current_user)):
    rows = await db.list_mcp_servers(user["id"])
    return [_mcp_out(r) for r in rows]


@router.post("/mcp-servers", response_model=MCPServerOut)
async def create_mcp(body: MCPServerIn, user: dict = Depends(get_current_user)):
    from ..runtime import get_hub
    row = await db.create_mcp_server(user["id"], **body.model_dump())
    hub = get_hub()
    if hub:
        await hub.invalidate(user["id"])
    return _mcp_out(row)


@router.patch("/mcp-servers/{server_id}", response_model=MCPServerOut)
async def update_mcp(server_id: str, body: MCPServerUpdate, user: dict = Depends(get_current_user)):
    from ..runtime import get_hub
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    row = await db.update_mcp_server(user["id"], server_id, **fields)
    if not row:
        raise HTTPException(status_code=404, detail="mcp server not found")
    hub = get_hub()
    if hub:
        await hub.invalidate(user["id"])
    return _mcp_out(row)


@router.delete("/mcp-servers/{server_id}")
async def delete_mcp(server_id: str, user: dict = Depends(get_current_user)):
    from ..runtime import get_hub
    ok = await db.delete_mcp_server(user["id"], server_id)
    if not ok:
        raise HTTPException(status_code=404, detail="mcp server not found")
    hub = get_hub()
    if hub:
        await hub.invalidate(user["id"])
    return {"ok": True}


@router.post("/mcp-servers/import", response_model=MCPImportResult)
async def import_mcp_json(body: MCPJsonIn, user: dict = Depends(get_current_user)):
    """直接粘贴标准 mcp.json（mcpServers）批量配置：按 name upsert，不清空其它既有配置。"""
    from ..mcp_config import merge_mcp_json
    from ..runtime import get_hub
    result = await merge_mcp_json(user["id"], body.raw, enabled=body.enabled)
    hub = get_hub()
    if hub:
        await hub.invalidate(user["id"])
    return MCPImportResult(**result)


@router.get("/mcp-servers/export")
async def export_mcp_json(user: dict = Depends(get_current_user)):
    """把当前用户全部 MCP 配置导出为标准 mcp.json 文本（用于编辑框回显）。"""
    from ..mcp_config import servers_to_mcp_json
    rows = await db.list_mcp_servers(user["id"])
    return {"json": servers_to_mcp_json(rows)}


@router.get("/mcp-effective")
async def mcp_effective(user: dict = Depends(get_current_user)):
    from ..runtime import get_hub
    hub = get_hub()
    if not hub:
        return {"tools": []}
    # 工具与模型无关，但 runtime 构建会顺带解析模型；模型不可用时不应让本接口 500。
    try:
        ur = await hub.get_user_runtime(user["id"])
        return {"tools": [t.name for t in ur.registry.tools]}
    except Exception as e:  # noqa: BLE001
        from ..builtin_tools import build_builtin_tools
        try:
            names = [t.name for t in build_builtin_tools(user["id"])]
        except Exception:
            names = []
        return {"tools": names, "error": f"{type(e).__name__}: {e}"}


@router.get("/llm-models")
async def llm_models(user: dict = Depends(get_current_user)):
    """可选模型列表：直接用用户配置的模型（仅暴露可用的配置）。"""
    from .. import db as _db
    rows = await _db.list_llm_configs(user["id"])
    models = []
    for r in rows:
        if not (r.get("api_key") and r.get("model") and r.get("base_url")):
            continue
        models.append({
            "id": r["id"],
            "label": r.get("label") or r.get("model"),
            "model": r.get("model", ""),
            "wire_api": r.get("wire_api", ""),
            "is_default": bool(r.get("is_default")),
            "source": "user",
        })
    return {"models": models}
